from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from threading import RLock
from typing import Any

from app.v5.models import CarryOpportunity, MarketLegSnapshot
from app.v5.research import build_carry_opportunity


class JsonlResearchSink:
    """Append-only local research sink; it has no database or exchange handle."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


class V5CarryShadowCollector:
    """Collect synchronized public carry telemetry without mutation capability.

    Inputs are already-observed public market snapshots. The collector owns no
    private client, portfolio service, risk service, capacity manager or order
    adapter, so an observation cannot become an exchange action.
    """

    def __init__(
        self,
        settings: Any,
        sink: Any,
        *,
        cadence_seconds: int | None = None,
        notionals: tuple[Decimal, ...] = (
            Decimal("100"), Decimal("200"), Decimal("500"), Decimal("1000"),
        ),
    ) -> None:
        if not bool(settings.v5_alpha_shadow_only):
            raise ValueError("V5 collector requires shadow-only mode")
        self.settings = settings
        self.sink = sink
        self.cadence_seconds = cadence_seconds or settings.v5_shadow_cadence_seconds
        self.notionals = notionals
        self._last_at: dict[str, datetime] = {}
        self._quotes: dict[str, dict[str, Any]] = {}
        self._metrics: Counter[str] = Counter()
        self._lock = RLock()

    def observe(
        self,
        *,
        symbol: str,
        spot: MarketLegSnapshot,
        perp: MarketLegSnapshot,
        current_funding_rate: Decimal | None,
        predicted_funding_rate: Decimal | None,
        next_funding_time: datetime | None,
        funding_interval_hours: Decimal | None,
        historical_funding: dict[str, Decimal | None] | None = None,
        funding_persistence: dict[str, Decimal | None] | None = None,
    ) -> CarryOpportunity | None:
        with self._lock:
            observed_at = max(spot.received_at, perp.received_at)
            last = self._last_at.get(symbol)
            if last is not None and (observed_at - last).total_seconds() < self.cadence_seconds:
                return None
            fees = {
                "spot_maker": self.settings.v5_spot_maker_fee_bps,
                "spot_taker": self.settings.v5_spot_taker_fee_bps,
                "perp_maker": self.settings.v5_perp_maker_fee_bps,
                "perp_taker": self.settings.v5_perp_taker_fee_bps,
            }
            opportunity = build_carry_opportunity(
                symbol=symbol,
                spot=spot,
                perp=perp,
                current_funding_rate=current_funding_rate,
                predicted_funding_rate=predicted_funding_rate,
                next_funding_time=next_funding_time,
                funding_interval_hours=funding_interval_hours,
                historical_funding=historical_funding,
                funding_persistence=funding_persistence,
                account_fees_bps=fees,
            )
            self._last_at[symbol] = observed_at
            self._metrics["opportunities"] += 1
            self._metrics["incomplete_opportunities"] += int(bool(opportunity.blockers))
            self._write({
                "record_type": "CARRY_OPPORTUNITY",
                **opportunity.model_dump(mode="json"),
            })
            self._capture_hypothetical_quotes(opportunity)
            return opportunity

    def _capture_hypothetical_quotes(self, opportunity: CarryOpportunity) -> None:
        if opportunity.spot_bid is None or opportunity.perp_ask is None:
            self._metrics["hypothetical_quote_skipped"] += 1
            return
        for notional in self.notionals:
            quote_id = f"{opportunity.opportunity_id}:{notional}"
            row = {
                "record_type": "HYPOTHETICAL_MAKER_QUOTE",
                "quote_id": quote_id,
                "opportunity_id": str(opportunity.opportunity_id),
                "timestamp": opportunity.timestamp.isoformat(),
                "symbol": opportunity.symbol,
                "notional_usdt": str(notional),
                "spot_buy_quote": str(opportunity.spot_bid),
                "perp_sell_quote": str(opportunity.perp_ask),
                "fill_assumed": False,
                "order_submitted": False,
                "adverse_selection_bps": None,
            }
            self._quotes[quote_id] = row
            self._write(row)
            self._metrics["hypothetical_quotes"] += 1

    def record_post_quote_market(
        self,
        *,
        quote_id: str,
        observed_at: datetime,
        spot_mid: Decimal,
        perp_mid: Decimal,
    ) -> dict[str, Any] | None:
        """Attach later market movement; this records no fill or order state."""
        with self._lock:
            quote = self._quotes.get(quote_id)
            if quote is None:
                return None
            if observed_at <= datetime.fromisoformat(str(quote["timestamp"])):
                raise ValueError("post-quote observation must be later than the quote")
            spot_quote = Decimal(str(quote["spot_buy_quote"]))
            perp_quote = Decimal(str(quote["perp_sell_quote"]))
            # Positive means the market moved against the intended long-spot /
            # short-perp maker pair after the hypothetical quote.
            spot_adverse = (spot_quote / spot_mid - Decimal("1")) * Decimal("10000")
            perp_adverse = (perp_mid / perp_quote - Decimal("1")) * Decimal("10000")
            payload = {
                "record_type": "HYPOTHETICAL_QUOTE_OUTCOME",
                "quote_id": quote_id,
                "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
                "spot_mid": str(spot_mid),
                "perp_mid": str(perp_mid),
                "combined_adverse_selection_bps": str(spot_adverse + perp_adverse),
                "fill_observed": False,
                "order_submitted": False,
            }
            self._write(payload)
            self._metrics["post_quote_observations"] += 1
            return payload

    def _write(self, payload: dict[str, Any]) -> None:
        writer = getattr(self.sink, "write", None)
        if not callable(writer):
            raise TypeError("V5 shadow sink must provide write(payload)")
        writer(payload)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "shadow_only": True,
                "opportunities": self._metrics["opportunities"],
                "incomplete_opportunities": self._metrics["incomplete_opportunities"],
                "hypothetical_quotes": self._metrics["hypothetical_quotes"],
                "post_quote_observations": self._metrics["post_quote_observations"],
                "exchange_mutations": 0,
                "capacity_mutations": 0,
                "production_risk_mutations": 0,
                "database_mutations": 0,
            }
