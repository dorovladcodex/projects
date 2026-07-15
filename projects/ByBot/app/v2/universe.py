from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from threading import RLock
from typing import Any, Callable, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import Settings
from app.models import Symbol
from app.v2.models import UniverseInstrument, UniverseState, UniverseStatus


class UniverseClient(Protocol):
    def inspect_symbol(self, symbol: Symbol) -> UniverseInstrument: ...


class BybitPublicUniverseClient:
    """Bounded public REST metadata client; it has no private/order methods."""

    def __init__(
        self,
        base_url: str = "https://api.bybit.com",
        *,
        timeout_seconds: float = 5,
        http_get: Callable[[str, dict[str, str], float], dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._http_get = http_get or _public_get

    def inspect_symbol(self, symbol: Symbol) -> UniverseInstrument:
        scope = {"category": "linear", "symbol": symbol.value}
        instrument = self._request("/v5/market/instruments-info", scope)
        ticker = self._request("/v5/market/tickers", scope)
        book = self._request(
            "/v5/market/orderbook", {**scope, "limit": "50"}
        )
        item = _first(instrument)
        tick = _first(ticker)
        result = book.get("result") or {}
        bids = result.get("b") or []
        asks = result.get("a") or []
        bid = _decimal((bids[0] if bids else [tick.get("bid1Price"), 0])[0])
        ask = _decimal((asks[0] if asks else [tick.get("ask1Price"), 0])[0])
        if bid <= 0 or ask <= 0 or ask < bid:
            raise ValueError("invalid bid/ask")
        midpoint = (bid + ask) / Decimal("2")
        spread = (ask - bid) / midpoint * Decimal("10000")
        lot = item.get("lotSizeFilter") or {}
        price_filter = item.get("priceFilter") or {}
        leverage = item.get("leverageFilter") or {}
        launch_ms = int(item.get("launchTime") or 0)
        timestamp_ms = int(result.get("ts") or ticker.get("time") or 0)
        now = datetime.now(timezone.utc)
        return UniverseInstrument(
            symbol=symbol,
            exists=True,
            status=str(item.get("status") or ""),
            category=str(item.get("contractType") or "linear").lower(),
            settle_coin=str(item.get("settleCoin") or ""),
            min_order_qty=_decimal(lot.get("minOrderQty")),
            qty_step=_decimal(lot.get("qtyStep")),
            min_notional_value=_decimal(lot.get("minNotionalValue"), "0"),
            min_leverage=_decimal(leverage.get("minLeverage"), "1"),
            max_leverage=_decimal(leverage.get("maxLeverage"), "1"),
            leverage_step=_decimal(leverage.get("leverageStep"), "0.01"),
            tick_size=_decimal(price_filter.get("tickSize")),
            turnover_24h=_decimal(tick.get("turnover24h"), "0"),
            spread_bps=spread,
            bid_depth_usdt=_depth(bids, midpoint),
            ask_depth_usdt=_depth(asks, midpoint),
            launch_time=(
                datetime.fromtimestamp(launch_ms / 1000, tz=timezone.utc)
                if launch_ms else None
            ),
            market_timestamp=(
                datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
                if timestamp_ms else now
            ),
        )

    def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        payload = self._http_get(f"{self.base_url}{path}", params, self.timeout_seconds)
        if int(payload.get("retCode", -1)) != 0:
            raise ValueError(f"Bybit public request failed: retCode={payload.get('retCode')}")
        return payload


class SymbolUniverseService:
    def __init__(
        self,
        settings: Settings,
        client: UniverseClient,
        repository: Any | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.repository = repository
        self._lock = RLock()
        self.statuses: dict[Symbol, UniverseStatus] = {}
        self.last_refresh_at: datetime | None = None

    @property
    def accepted_symbols(self) -> tuple[Symbol, ...]:
        with self._lock:
            return tuple(
                symbol for symbol, status in self.statuses.items() if status.accepted
            )

    def refresh(self, *, now: datetime | None = None) -> dict[Symbol, UniverseStatus]:
        checked_at = now or datetime.now(timezone.utc)
        updated: dict[Symbol, UniverseStatus] = {}
        for value in self.settings.v2_universe_symbols:
            symbol = Symbol(value)
            try:
                instrument = self.client.inspect_symbol(symbol)
                reasons = self._rejection_reasons(instrument, checked_at)
                status = UniverseStatus(
                    symbol=symbol,
                    state=UniverseState.REJECTED if reasons else UniverseState.ACCEPTED,
                    accepted=not reasons,
                    reasons=reasons,
                    instrument=instrument,
                    checked_at=checked_at,
                )
            except Exception as exc:
                status = UniverseStatus(
                    symbol=symbol,
                    state=UniverseState.DATA_UNAVAILABLE,
                    accepted=False,
                    reasons=[f"instrument inspection failed: {type(exc).__name__}"],
                    checked_at=checked_at,
                )
            updated[symbol] = status
            saver = getattr(self.repository, "save_v2_universe_status", None)
            if callable(saver):
                saver(status)
        with self._lock:
            self.statuses = updated
            self.last_refresh_at = checked_at
        return dict(updated)

    def get(self, symbol: Symbol) -> UniverseStatus | None:
        with self._lock:
            return self.statuses.get(symbol)

    def _rejection_reasons(
        self, instrument: UniverseInstrument, now: datetime
    ) -> list[str]:
        reasons: list[str] = []
        if not instrument.exists:
            reasons.append("instrument does not exist")
        if instrument.status != "Trading":
            reasons.append("instrument status is not Trading")
        if instrument.settle_coin != "USDT":
            reasons.append("settle coin is not USDT")
        # Bybit calls USDT perpetuals LinearPerpetual; accept either spelling.
        if "linear" not in instrument.category.lower():
            reasons.append("instrument category is not linear")
        if instrument.turnover_24h < self.settings.v2_min_turnover_24h_usdt:
            reasons.append("24h turnover below minimum")
        if instrument.spread_bps > self.settings.v2_max_spread_bps:
            reasons.append("spread above maximum")
        if min(instrument.bid_depth_usdt, instrument.ask_depth_usdt) < (
            self.settings.v2_min_orderbook_depth_usdt
        ):
            reasons.append("order-book depth below minimum")
        age = (now - instrument.market_timestamp).total_seconds()
        if age > self.settings.v2_market_stale_seconds:
            reasons.append("market data is stale")
        desired_leverage = self.settings.v2_leverage_for_symbol(instrument.symbol.value)
        if desired_leverage > instrument.max_leverage:
            reasons.append("configured leverage exceeds instrument maximum")
        return reasons

    def as_payload(self) -> dict[str, Any]:
        return {
            "last_refresh_at": self.last_refresh_at.isoformat()
            if self.last_refresh_at else None,
            "accepted": [item.value for item in self.accepted_symbols],
            "symbols": {
                symbol.value: status.model_dump(mode="json")
                for symbol, status in self.statuses.items()
            },
        }


def _public_get(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "ByBot/2"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed configured API
        return json.loads(response.read().decode("utf-8"))


def _first(payload: dict[str, Any]) -> dict[str, Any]:
    rows = (payload.get("result") or {}).get("list") or []
    if not rows or not isinstance(rows[0], dict):
        raise ValueError("Bybit response has no rows")
    return rows[0]


def _decimal(value: object, default: str | None = None) -> Decimal:
    if value in (None, ""):
        if default is None:
            raise ValueError("required decimal is missing")
        return Decimal(default)
    return Decimal(str(value))


def _depth(rows: list[list[str]], midpoint: Decimal) -> Decimal:
    return sum(
        (_decimal(row[0]) * _decimal(row[1]) for row in rows if len(row) >= 2),
        Decimal("0"),
    ) if midpoint > 0 else Decimal("0")
