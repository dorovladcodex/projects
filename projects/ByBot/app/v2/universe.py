from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor
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


class UniverseInspectionError(ValueError):
    """Sanitized, symbol-local public market inspection failure."""

    def __init__(
        self,
        code: str,
        *,
        field: str | None = None,
        value: object | None = None,
        detail: str | None = None,
    ) -> None:
        self.code = code
        self.field = field
        self.value = value
        self.detail = detail
        super().__init__(self.reason)

    @property
    def reason(self) -> str:
        attributes: list[str] = []
        if self.field:
            attributes.append(f"field={self.field}")
        if self.value is not None:
            attributes.append(f"value={_sanitized_value(self.value)}")
        if self.detail:
            attributes.append(f"detail={_sanitized_value(self.detail)}")
        return f"{self.code}: {' '.join(attributes)}" if attributes else self.code


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
        instrument = self._request(
            "/v5/market/instruments-info",
            scope,
            error_code="instrument_not_available_on_demo",
        )
        item = _first(
            instrument,
            error_code="instrument_not_available_on_demo",
            symbol=symbol,
        )
        status = str(item.get("status") or "")
        if status != "Trading":
            raise UniverseInspectionError(
                "instrument_not_available_on_demo",
                field="status",
                value=status or "<empty>",
                detail=f"symbol={symbol.value}",
            )
        ticker = self._request(
            "/v5/market/tickers", scope, error_code="ticker_not_available"
        )
        book = self._request(
            "/v5/market/orderbook",
            {**scope, "limit": "50"},
            error_code="insufficient_order_book",
        )
        tick = _first(ticker, error_code="ticker_not_available", symbol=symbol)
        result = book.get("result") or {}
        bids = result.get("b") or []
        asks = result.get("a") or []
        bid_value = (bids[0] if bids else [tick.get("bid1Price")])[0]
        ask_value = (asks[0] if asks else [tick.get("ask1Price")])[0]
        bid = _market_decimal(bid_value, "bid")
        ask = _market_decimal(ask_value, "ask")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise UniverseInspectionError(
                "empty_bid_ask" if bid <= 0 or ask <= 0 else "invalid_bid_ask",
                detail=f"bid={_sanitized_value(bid_value)} ask={_sanitized_value(ask_value)}",
            )
        midpoint = (bid + ask) / Decimal("2")
        spread = (ask - bid) / midpoint * Decimal("10000")
        lot = item.get("lotSizeFilter") or {}
        price_filter = item.get("priceFilter") or {}
        leverage = item.get("leverageFilter") or {}
        launch_ms = _optional_integer(item.get("launchTime"))
        timestamp_ms = _optional_integer(
            result.get("ts") or ticker.get("time") or 0
        )
        now = datetime.now(timezone.utc)
        return UniverseInstrument(
            symbol=symbol,
            exists=True,
            status=status,
            category=str(item.get("contractType") or "linear").lower(),
            settle_coin=str(item.get("settleCoin") or ""),
            min_order_qty=_required_positive_decimal(
                lot.get("minOrderQty"), "lotSizeFilter.minOrderQty"
            ),
            qty_step=_required_positive_decimal(
                lot.get("qtyStep"), "lotSizeFilter.qtyStep"
            ),
            min_notional_value=_required_nonnegative_decimal(
                lot.get("minNotionalValue"), "lotSizeFilter.minNotionalValue"
            ),
            min_leverage=_required_positive_decimal(
                leverage.get("minLeverage"), "leverageFilter.minLeverage"
            ),
            max_leverage=_required_positive_decimal(
                leverage.get("maxLeverage"), "leverageFilter.maxLeverage"
            ),
            leverage_step=_required_positive_decimal(
                leverage.get("leverageStep"), "leverageFilter.leverageStep"
            ),
            tick_size=_required_positive_decimal(
                price_filter.get("tickSize"), "priceFilter.tickSize"
            ),
            turnover_24h=_required_nonnegative_decimal(
                tick.get("turnover24h"), "ticker.turnover24h"
            ),
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

    def _request(
        self,
        path: str,
        params: dict[str, str],
        *,
        error_code: str = "market_data_unavailable",
    ) -> dict[str, Any]:
        payload = self._http_get(f"{self.base_url}{path}", params, self.timeout_seconds)
        if int(payload.get("retCode", -1)) != 0:
            raise UniverseInspectionError(
                error_code,
                field="symbol",
                value=params.get("symbol") or "<unspecified>",
                detail=(
                    f"retCode={payload.get('retCode')} "
                    f"retMsg={payload.get('retMsg') or '<empty>'}"
                ),
            )
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
        symbols = [Symbol(value) for value in self.settings.v2_universe_symbols]

        def inspect(symbol: Symbol) -> UniverseStatus:
            try:
                instrument = self.client.inspect_symbol(symbol)
                reasons = self._rejection_reasons(instrument, checked_at)
                return UniverseStatus(
                    symbol=symbol,
                    state=UniverseState.REJECTED if reasons else UniverseState.ACCEPTED,
                    accepted=not reasons,
                    reasons=reasons,
                    instrument=instrument,
                    checked_at=checked_at,
                )
            except UniverseInspectionError as exc:
                return UniverseStatus(
                    symbol=symbol,
                    state=UniverseState.DATA_UNAVAILABLE,
                    accepted=False,
                    reasons=[exc.reason],
                    checked_at=checked_at,
                )
            except Exception as exc:
                return UniverseStatus(
                    symbol=symbol,
                    state=UniverseState.DATA_UNAVAILABLE,
                    accepted=False,
                    reasons=[
                        "instrument_inspection_unavailable: "
                        f"error_type={type(exc).__name__}"
                    ],
                    checked_at=checked_at,
                )

        # Each symbol is independent and public-only. Parallel inspection keeps
        # the 17-symbol bootstrap bounded by the slowest few symbols instead of
        # serializing 51 REST requests on the FastAPI lifespan.
        workers = min(self.settings.v2_startup_universe_workers, len(symbols))
        with ThreadPoolExecutor(
            max_workers=max(1, workers),
            thread_name_prefix="v2-universe",
        ) as pool:
            inspected = list(pool.map(inspect, symbols))

        # Persist deterministically on the caller thread. Repository sessions
        # are intentionally not shared by the inspection workers.
        for symbol, status in zip(symbols, inspected, strict=True):
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
            reasons.append("instrument_not_available_on_demo: exists=false")
        if instrument.status != "Trading":
            reasons.append(
                f"instrument_not_available_on_demo: status={instrument.status or '<empty>'}"
            )
        if instrument.settle_coin != "USDT":
            reasons.append(f"settle_coin_not_usdt: value={instrument.settle_coin}")
        # Bybit calls USDT perpetuals LinearPerpetual; accept either spelling.
        if "linear" not in instrument.category.lower():
            reasons.append(f"instrument_category_not_linear: value={instrument.category}")
        if instrument.turnover_24h < self.settings.v2_min_turnover_24h_usdt:
            reasons.append(
                "insufficient_turnover: "
                f"actual={instrument.turnover_24h} "
                f"minimum={self.settings.v2_min_turnover_24h_usdt}"
            )
        if instrument.spread_bps > self.settings.v2_max_spread_bps:
            reasons.append(
                "spread_above_maximum: "
                f"actual={instrument.spread_bps} maximum={self.settings.v2_max_spread_bps}"
            )
        if min(instrument.bid_depth_usdt, instrument.ask_depth_usdt) < (
            self.settings.v2_min_orderbook_depth_usdt
        ):
            reasons.append(
                "insufficient_order_book: "
                f"bid_depth={instrument.bid_depth_usdt} "
                f"ask_depth={instrument.ask_depth_usdt} "
                f"minimum={self.settings.v2_min_orderbook_depth_usdt}"
            )
        age = (now - instrument.market_timestamp).total_seconds()
        if age > self.settings.v2_market_stale_seconds:
            reasons.append(
                f"stale_market_data: age_seconds={age:.3f} "
                f"maximum={self.settings.v2_market_stale_seconds}"
            )
        desired_leverage = self.settings.v2_leverage_for_symbol(instrument.symbol.value)
        if desired_leverage > instrument.max_leverage:
            reasons.append(
                "configured_leverage_exceeds_instrument_maximum: "
                f"configured={desired_leverage} maximum={instrument.max_leverage}"
            )
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


def _first(
    payload: dict[str, Any], *, error_code: str, symbol: Symbol
) -> dict[str, Any]:
    rows = (payload.get("result") or {}).get("list") or []
    if not rows or not isinstance(rows[0], dict):
        raise UniverseInspectionError(
            error_code, field="symbol", value=symbol.value, detail="response has no rows"
        )
    return rows[0]


def _decimal(value: object, *, field: str) -> Decimal:
    if value in (None, ""):
        raise UniverseInspectionError("missing_filter_field", field=field)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise UniverseInspectionError(
            "invalid_numeric_field", field=field, value=value
        ) from None
    if not parsed.is_finite():
        raise UniverseInspectionError(
            "invalid_numeric_field", field=field, value=value
        )
    return parsed


def _required_positive_decimal(value: object, field: str) -> Decimal:
    parsed = _decimal(value, field=field)
    if parsed <= 0:
        raise UniverseInspectionError("invalid_numeric_field", field=field, value=value)
    return parsed


def _required_nonnegative_decimal(value: object, field: str) -> Decimal:
    parsed = _decimal(value, field=field)
    if parsed < 0:
        raise UniverseInspectionError("invalid_numeric_field", field=field, value=value)
    return parsed


def _market_decimal(value: object, field: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise UniverseInspectionError(
            "invalid_numeric_field", field=field, value=value
        ) from None
    return parsed if parsed.is_finite() else Decimal("0")


def _optional_integer(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        parsed = Decimal(str(value))
        return int(parsed) if parsed.is_finite() and parsed >= 0 else 0
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return 0


def _depth(rows: list[list[str]], midpoint: Decimal) -> Decimal:
    if midpoint <= 0:
        return Decimal("0")
    total = Decimal("0")
    for row in rows:
        if len(row) < 2:
            continue
        try:
            price = Decimal(str(row[0]))
            quantity = Decimal(str(row[1]))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if price.is_finite() and quantity.is_finite() and price > 0 and quantity > 0:
            total += price * quantity
    return total


def _sanitized_value(value: object) -> str:
    text = " ".join(str(value).split())[:120]
    return text or "<empty>"
