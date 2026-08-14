from __future__ import annotations

import time
from statistics import median
from typing import Any

from app.basis.models import BasisObservation, CurveAlert, Quote
from app.history.client import BybitHistoryClient

DEPTH_BAND_BPS = 10.0


def _book_quote(
    client: BybitHistoryClient, category: str, symbol: str, band_bps: float = DEPTH_BAND_BPS
) -> Quote | None:
    payload = client.get(
        "/v5/market/orderbook",
        {"category": category, "symbol": symbol, "limit": "50"},
    )
    result = payload.get("result") or {}
    bids, asks = result.get("b") or [], result.get("a") or []
    if not bids or not asks:
        return None

    bid, ask = float(bids[0][0]), float(asks[0][0])
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2
    band = mid * band_bps / 10_000.0
    depth = sum(float(p) * float(q) for p, q in bids if float(p) >= mid - band)
    depth += sum(float(p) * float(q) for p, q in asks if float(p) <= mid + band)
    return Quote(
        symbol=symbol,
        mid=mid,
        spread_bps=(ask - bid) / mid * 10_000.0,
        depth_usd=depth,
    )


class BasisMonitor:
    """Reads the dated-futures curve and prices the carry against it."""

    def __init__(self, client: BybitHistoryClient, *, now_ms=None) -> None:
        self.client = client
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def dated_contracts(self) -> list[dict[str, Any]]:
        payload = self.client.get(
            "/v5/market/instruments-info", {"category": "linear", "limit": "1000"}
        )
        rows = (payload.get("result") or {}).get("list") or []
        return [row for row in rows if row.get("contractType") == "LinearFutures"]

    def observe(
        self, *, reference_kind: str = "perp", base_coins: set[str] | None = None
    ) -> list[BasisObservation]:
        """Quote every dated contract against its reference leg.

        The perpetual is the default reference: both legs are then futures
        products at the same fee, which avoids the 10 bps per side spot tax
        that made the spot-based carry unviable.
        """
        if reference_kind not in ("perp", "spot"):
            raise ValueError(f"unsupported reference: {reference_kind}")

        now = self._now_ms()
        references: dict[str, Quote | None] = {}
        observations: list[BasisObservation] = []

        for row in self.dated_contracts():
            symbol = row.get("symbol") or ""
            base = row.get("baseCoin") or ""
            if base_coins and base not in base_coins:
                continue
            delivery = int(row.get("deliveryTime") or 0)
            if delivery <= now:
                continue

            underlying = f"{base}USDT"
            if underlying not in references:
                category = "linear" if reference_kind == "perp" else "spot"
                references[underlying] = _book_quote(self.client, category, underlying)
            reference = references[underlying]
            if reference is None:
                continue

            future = _book_quote(self.client, "linear", symbol)
            if future is None:
                continue

            observations.append(
                BasisObservation(
                    observed_at_ms=now,
                    base_coin=base,
                    future=future,
                    reference=reference,
                    reference_kind=reference_kind,
                    delivery_ms=delivery,
                )
            )

        observations.sort(key=lambda item: (item.base_coin, item.delivery_ms))
        return observations


def curve_alerts(
    history: dict[str, list[float]],
    current: list[BasisObservation],
    *,
    threshold_bps: float = 100.0,
    minimum_observations: int = 8,
) -> list[CurveAlert]:
    """Flag contracts whose annualised basis has left its own observed range.

    The comparison is per contract against its own median, not against a fixed
    band: the curve sits near 440 bps today, but that level is a market rate
    that drifts, and a threshold hard-coded to today's level would age badly.
    """
    alerts: list[CurveAlert] = []
    for observation in current:
        samples = history.get(observation.future.symbol) or []
        if len(samples) < minimum_observations:
            continue
        centre = median(samples)
        deviation = observation.annualised_bps - centre
        if abs(deviation) < threshold_bps:
            continue
        alerts.append(
            CurveAlert(
                symbol=observation.future.symbol,
                annualised_bps=observation.annualised_bps,
                median_bps=centre,
                deviation_bps=deviation,
                observations=len(samples),
                direction="rich" if deviation > 0 else "cheap",
            )
        )
    alerts.sort(key=lambda alert: -abs(alert.deviation_bps))
    return alerts
