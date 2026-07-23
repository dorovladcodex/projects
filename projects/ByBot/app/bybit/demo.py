from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import hmac
import json
import time
from time import perf_counter
from threading import RLock
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

import websockets

from app.config import ExecutionMode, Settings
from app.models import (
    DemoExecutionRecord,
    DemoExecutionState,
    DemoFill,
    ExecutionEnvironment,
    MarketSnapshot,
    NewsClassification,
    NewsSignalAction,
    NewsSignalCandidate,
    Side,
    SignalRiskPreview,
    Symbol,
)


DEMO_REST_URL = "https://api-demo.bybit.com"
DEMO_PRIVATE_WS_URL = "wss://stream-demo.bybit.com"
BOT_ORDER_PURPOSES = {"entry", "close", "emergency"}
TERMINAL_DEMO_STATES = {
    DemoExecutionState.DEMO_CLOSED,
    DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE,
    DemoExecutionState.DEMO_FAILED,
    DemoExecutionState.DEMO_NOT_SUBMITTED,
    DemoExecutionState.DEMO_ORDER_CANCELLED,
    DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION,
    DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
    DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED,
}
CANONICAL_EXIT_ATTRIBUTIONS = {
    "take_profit", "stop_loss", "strategy_exit", "stale_signal",
    "maximum_holding_time", "reconciliation_close", "manual_external_close",
    "forced_cleanup",
    "unattributed_external_close",
}


def canonical_exit_attribution(reason: str | None) -> str:
    normalized = str(reason or "").strip().casefold()
    aliases = {
        "invalidated_setup": "strategy_exit",
        "runner_cleanup": "forced_cleanup",
        "protection_failure": "forced_cleanup",
        "emergency_close": "forced_cleanup",
        "exchange_close": "reconciliation_close",
        "exchange_generated_tp": "take_profit",
        "exchange_generated_sl": "stop_loss",
        "external_close": "manual_external_close",
    }
    value = aliases.get(normalized, normalized)
    return value if value in CANONICAL_EXIT_ATTRIBUTIONS else "unattributed_external_close"


def classify_exchange_close(
    item: dict[str, Any],
    *,
    durable_reason: str | None = None,
    allow_generic_reduce_only: bool = False,
) -> str | None:
    """Classify close metadata without using price proximity."""
    stop_type = str(item.get("stopOrderType") or "").casefold()
    create_type = str(item.get("createType") or "").casefold()
    if stop_type == "stoploss" or create_type == "createbystoploss":
        return "stop_loss"
    if stop_type == "takeprofit" or create_type == "createbytakeprofit":
        return "take_profit"
    if create_type == "createbyclosing":
        return "manual_external_close"
    if durable_reason:
        existing = canonical_exit_attribution(durable_reason)
        if existing != "unattributed_external_close":
            return existing
    if allow_generic_reduce_only and (
        str(item.get("reduceOnly") or "").lower() == "true"
        or _decimal(item.get("closedSize"), default="0") > 0
    ):
        return "manual_external_close"
    return None


def attribute_exchange_close(
    record: DemoExecutionRecord, item: dict[str, Any], *, source: str,
) -> str:
    """Set canonical exit attribution from exchange metadata, never price alone."""
    attribution = classify_exchange_close(
        item,
        durable_reason=record.exit_attribution or record.close_reason,
        allow_generic_reduce_only=True,
    ) or "unattributed_external_close"
    evidence = {
        "source": source,
        "order_id": str(item.get("orderId") or record.close_order_id or "") or None,
        "execution_id": str(item.get("execId") or "") or None,
        "order_link_id": str(item.get("orderLinkId") or "") or None,
        "stop_order_type": str(item.get("stopOrderType") or "") or None,
        "create_type": str(item.get("createType") or "") or None,
        "reduce_only": bool(item.get("reduceOnly")),
        "close_on_trigger": bool(item.get("closeOnTrigger")),
        "trigger_price": str(item.get("triggerPrice") or "") or None,
        "side": str(item.get("side") or "") or None,
        "position_idx": item.get("positionIdx"),
        "owned_quantity": str(record.accepted_quantity),
    }
    record.exit_attribution = attribution
    record.close_reason = attribution
    record.exit_attribution_evidence = evidence
    record.attribution_failure_reason = (
        "exchange close metadata did not identify an owned strategy or TP/SL close"
        if attribution == "unattributed_external_close" else None
    )
    return attribution


class DemoSafetyError(RuntimeError):
    pass


class DemoExchangeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        ret_code: int | None = None,
        ret_msg: str | None = None,
    ) -> None:
        super().__init__(message)
        self.ret_code = ret_code
        self.ret_msg = ret_msg


@dataclass(frozen=True)
class InstrumentRules:
    symbol: Symbol
    status: str
    qty_step: Decimal
    min_order_qty: Decimal
    min_notional_value: Decimal
    tick_size: Decimal
    min_leverage: Decimal
    max_leverage: Decimal
    leverage_step: Decimal


@dataclass(frozen=True)
class ProtectionVerificationOutcome:
    record: DemoExecutionRecord
    verified: bool
    terminalizing: bool
    attempts: int


@dataclass(frozen=True)
class CanaryMinimumOrderPlan:
    """Exchange-minimum order plan bounded by an explicit canary budget.

    All values remain ``Decimal`` so an exchange quantity is never made valid
    through a lossy float conversion.  ``max_notional_usdt`` is a hard ceiling,
    not a target position size.
    """

    symbol: Symbol
    instrument_status: str
    min_order_qty: Decimal
    qty_step: Decimal
    min_notional_value: Decimal
    reference_price: Decimal
    calculated_order_qty: Decimal
    estimated_notional: Decimal
    safety_buffer_pct: Decimal
    buffered_required_notional: Decimal
    max_notional_usdt: Decimal
    rules_fingerprint: str


def calculate_minimum_valid_canary_order(
    rules: InstrumentRules,
    reference_price: Decimal,
    max_notional_usdt: Decimal,
    *,
    safety_buffer_pct: Decimal = Decimal("5"),
) -> CanaryMinimumOrderPlan:
    """Calculate the smallest exchange-valid quantity within a hard budget.

    The minimum-notional-derived quantity and the exchange minimum quantity
    are both rounded *up* to ``qtyStep``.  The market-price buffer is used only
    for budget validation; it never increases the submitted quantity.
    """

    if not all(
        isinstance(value, Decimal)
        for value in (reference_price, max_notional_usdt, safety_buffer_pct)
    ):
        raise TypeError("canary financial inputs must use Decimal")
    if rules.status != "Trading":
        raise DemoSafetyError(f"{rules.symbol.value} is not Trading")
    if reference_price <= 0:
        raise DemoSafetyError("canary reference price is unavailable")
    if max_notional_usdt <= 0:
        raise DemoSafetyError("maximum canary budget must be positive")
    if safety_buffer_pct < 0:
        raise DemoSafetyError("canary safety buffer cannot be negative")
    if rules.qty_step <= 0 or rules.min_order_qty <= 0:
        raise DemoSafetyError("invalid exchange quantity rules")
    if rules.min_notional_value < 0:
        raise DemoSafetyError("invalid exchange minimum notional")

    quantity_for_notional = _step_round(
        rules.min_notional_value / reference_price,
        rules.qty_step,
        ROUND_UP,
    )
    required_quantity = _step_round(
        max(rules.min_order_qty, quantity_for_notional),
        rules.qty_step,
        ROUND_UP,
    )
    estimated_notional = required_quantity * reference_price
    buffered_required_notional = estimated_notional * (
        Decimal("1") + safety_buffer_pct / Decimal("100")
    )
    if buffered_required_notional > max_notional_usdt:
        raise DemoSafetyError(
            f"{rules.symbol.value} buffered exchange minimum exceeds the explicit "
            "maximum canary budget"
        )

    return CanaryMinimumOrderPlan(
        symbol=rules.symbol,
        instrument_status=rules.status,
        min_order_qty=rules.min_order_qty,
        qty_step=rules.qty_step,
        min_notional_value=rules.min_notional_value,
        reference_price=reference_price,
        calculated_order_qty=required_quantity,
        estimated_notional=estimated_notional,
        safety_buffer_pct=safety_buffer_pct,
        buffered_required_notional=buffered_required_notional,
        max_notional_usdt=max_notional_usdt,
        rules_fingerprint=canary_rules_fingerprint(rules),
    )


def canary_rules_fingerprint(rules: InstrumentRules) -> str:
    """Return a stable, non-secret fingerprint for race-safe rule validation."""

    payload = "|".join(
        (
            rules.symbol.value,
            rules.status,
            _format_decimal(rules.min_order_qty),
            _format_decimal(rules.qty_step),
            _format_decimal(rules.min_notional_value),
            _format_decimal(rules.tick_size),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canary_plan_payload(plan: CanaryMinimumOrderPlan) -> dict[str, str]:
    return {
        "symbol": plan.symbol.value,
        "instrument_status": plan.instrument_status,
        "min_order_qty": _format_decimal(plan.min_order_qty),
        "qty_step": _format_decimal(plan.qty_step),
        "min_notional_value": _format_decimal(plan.min_notional_value),
        "reference_price": _format_decimal(plan.reference_price),
        "calculated_quantity": _format_decimal(plan.calculated_order_qty),
        "estimated_notional": _format_decimal(plan.estimated_notional),
        "market_price_buffer_pct": _format_decimal(plan.safety_buffer_pct),
        "buffered_required_notional": _format_decimal(
            plan.buffered_required_notional
        ),
        "max_notional_usdt": _format_decimal(plan.max_notional_usdt),
        "rules_fingerprint": plan.rules_fingerprint,
    }


def revalidate_canary_order_plan(
    plan: CanaryMinimumOrderPlan,
    current_rules: InstrumentRules,
    current_reference_price: Decimal,
) -> CanaryMinimumOrderPlan:
    """Revalidate rules and price immediately before an exchange submission."""

    if current_rules.symbol != plan.symbol or (
        current_rules.status,
        current_rules.min_order_qty,
        current_rules.qty_step,
        current_rules.min_notional_value,
    ) != (
        "Trading",
        plan.min_order_qty,
        plan.qty_step,
        plan.min_notional_value,
    ):
        raise DemoSafetyError("exchange instrument rules changed before submission")
    if canary_rules_fingerprint(current_rules) != plan.rules_fingerprint:
        raise DemoSafetyError("exchange instrument rules changed before submission")
    return calculate_minimum_valid_canary_order(
        current_rules,
        current_reference_price,
        plan.max_notional_usdt,
        safety_buffer_pct=plan.safety_buffer_pct,
    )


class DemoExchangeClient(Protocol):
    base_url: str
    private_ws_url: str

    def verify_credentials(self) -> bool: ...
    def get_account_info(self) -> dict[str, Any]: ...
    def get_fee_rate(self, symbol: Symbol) -> tuple[Decimal, Decimal]: ...
    def get_instrument(self, symbol: Symbol) -> InstrumentRules: ...
    def get_positions(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]: ...
    def get_open_orders(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]: ...
    def get_order_history(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]: ...
    def get_executions(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]: ...
    def get_closed_pnl(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]: ...
    def set_leverage(self, symbol: Symbol, leverage: Decimal) -> dict[str, Any]: ...
    def create_order(self, payload: dict[str, str]) -> dict[str, Any]: ...
    def cancel_order(self, symbol: Symbol, order_id: str) -> dict[str, Any]: ...
    def set_trading_stop(
        self, symbol: Symbol, take_profit: Decimal, stop_loss: Decimal
    ) -> dict[str, Any]: ...


def validate_demo_domains(rest_url: str, private_ws_url: str) -> None:
    if rest_url.rstrip("/") != DEMO_REST_URL:
        raise DemoSafetyError(f"Demo REST domain must be exactly {DEMO_REST_URL}")
    if private_ws_url.rstrip("/") != DEMO_PRIVATE_WS_URL:
        raise DemoSafetyError(
            f"Demo private WebSocket domain must be exactly {DEMO_PRIVATE_WS_URL}"
        )


def validate_demo_order_execution_enabled(settings: Settings) -> None:
    """Fail closed unless the process has explicit Demo mutation authority."""

    validate_demo_domains(
        settings.bybit_private_demo_base_url,
        settings.bybit_private_demo_ws_url,
    )
    if settings.execution_mode != ExecutionMode.BYBIT_DEMO:
        raise DemoSafetyError("Execution mode is not BYBIT_DEMO")
    if not settings.bybit_demo_trading_enabled:
        raise DemoSafetyError("Demo trading is not explicitly enabled")
    if not settings.demo_order_execution_authorized:
        raise DemoSafetyError("explicit Demo order authorization is required")
    if settings.app_env.lower() != "demo" or settings.test_mode:
        raise DemoSafetyError("Demo execution requires APP_ENV=demo and TEST_MODE=false")
    if settings.bybit_live_trading_enabled or settings.bybit_enable_trading:
        raise DemoSafetyError("Live or generic Bybit trading flags are forbidden")
    if settings.bybit_env.value != "demo":
        raise DemoSafetyError("Authenticated Bybit environment is not Demo")
    if not settings.bybit_api_key or not settings.bybit_api_secret:
        raise DemoSafetyError("Demo API credentials are required")
    if not settings.v2_enabled and settings.demo_leverage != 1:
        raise DemoSafetyError("Demo leverage must be exactly 1")


def require_demo_execution(settings: Settings) -> None:
    """Backward-compatible name for the explicit mutation guard."""

    validate_demo_order_execution_enabled(settings)


def normalize_quantity(quantity: Decimal, rules: InstrumentRules) -> Decimal:
    normalized = _floor_to_step(quantity, rules.qty_step)
    if normalized < rules.min_order_qty:
        raise DemoSafetyError("normalized quantity is below minOrderQty")
    return normalized


def normalize_price(price: Decimal, rules: InstrumentRules, *, round_up: bool) -> Decimal:
    return _step_round(price, rules.tick_size, ROUND_UP if round_up else ROUND_DOWN)


def validate_order_notional(quantity: Decimal, price: Decimal, rules: InstrumentRules) -> None:
    if quantity * price < rules.min_notional_value:
        raise DemoSafetyError("normalized order notional is below minNotionalValue")


def deterministic_order_link_id(prefix: str, candidate_id: UUID | str, purpose: str) -> str:
    if purpose not in BOT_ORDER_PURPOSES:
        raise ValueError("unsupported order purpose")
    digest = hashlib.sha256(f"{candidate_id}:{purpose}".encode("utf-8")).hexdigest()[:20]
    return f"{prefix[:12]}-{purpose[0]}-{digest}"[:36]


def parse_instrument(payload: dict[str, Any], symbol: Symbol) -> InstrumentRules:
    result = payload.get("result", payload)
    items = result.get("list", []) if isinstance(result, dict) else []
    item = next(
        (row for row in items if isinstance(row, dict) and row.get("symbol") == symbol.value),
        None,
    )
    if item is None:
        raise DemoExchangeError(f"instrument info is missing for {symbol.value}")
    lot = item.get("lotSizeFilter") or {}
    price = item.get("priceFilter") or {}
    leverage = item.get("leverageFilter") or {}
    return InstrumentRules(
        symbol=symbol,
        status=str(item.get("status") or ""),
        qty_step=_decimal(lot.get("qtyStep")),
        min_order_qty=_decimal(lot.get("minOrderQty")),
        min_notional_value=_decimal(lot.get("minNotionalValue")),
        tick_size=_decimal(price.get("tickSize")),
        min_leverage=_decimal(leverage.get("minLeverage")),
        max_leverage=_decimal(leverage.get("maxLeverage")),
        leverage_step=_decimal(leverage.get("leverageStep")),
    )


def _position_leverages(
    positions: list[dict[str, Any]], symbol: Symbol
) -> tuple[Decimal, Decimal]:
    rows = [
        item for item in positions
        if not item.get("symbol") or str(item.get("symbol")) == symbol.value
    ]
    if not rows:
        raise DemoSafetyError(f"{symbol.value} leverage data is unavailable")
    for item in rows:
        if int(item.get("positionIdx") or 0) != 0:
            raise DemoSafetyError("hedge position mode is not supported")
    # Unified one-way mode normally returns one leverage value applying to both
    # sides. Explicit side-specific values are supported for deterministic tests
    # and future API response variants.
    row = rows[0]
    common = row.get("leverage")
    buy = _decimal(row.get("buyLeverage", common), default="0")
    sell = _decimal(row.get("sellLeverage", common), default="0")
    return buy, sell


class BybitDemoRestClient:
    """A Demo-only V5 adapter. Construction fails for every non-Demo domain."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = DEMO_REST_URL,
        private_ws_url: str = DEMO_PRIVATE_WS_URL,
        recv_window_ms: int = 5000,
        timeout_seconds: float = 10,
        http_request: Callable[
            [str, str, dict[str, str], bytes | None, float], dict[str, Any]
        ] | None = None,
    ) -> None:
        validate_demo_domains(base_url, private_ws_url)
        if not api_key or not api_secret:
            raise DemoSafetyError("Demo API credentials are required")
        self.api_key = api_key
        self._api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.private_ws_url = private_ws_url.rstrip("/")
        self.recv_window_ms = recv_window_ms
        self.timeout_seconds = timeout_seconds
        self._http_request = http_request or _url_request

    def verify_credentials(self) -> bool:
        self._request(
            "GET", "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": "USDT"},
        )
        return True

    def get_account_info(self) -> dict[str, Any]:
        data = self._request("GET", "/v5/account/info", {})
        result = data.get("result")
        return result if isinstance(result, dict) else {}

    def get_fee_rate(self, symbol: Symbol) -> tuple[Decimal, Decimal]:
        data = self._request(
            "GET", "/v5/account/fee-rate",
            {"category": "linear", "symbol": symbol.value},
        )
        rows = (data.get("result") or {}).get("list") or []
        row = next(
            (item for item in rows if str(item.get("symbol") or symbol.value) == symbol.value),
            None,
        )
        if row is None:
            raise DemoExchangeError("fee rate is unavailable for symbol")
        maker = _decimal(row.get("makerFeeRate")) * Decimal("10000")
        taker = _decimal(row.get("takerFeeRate")) * Decimal("10000")
        if maker < 0 or taker < 0:
            raise DemoExchangeError("negative fee rate is not supported")
        return maker, taker

    def get_instrument(self, symbol: Symbol) -> InstrumentRules:
        data = self._request(
            "GET", "/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol.value},
        )
        rules = parse_instrument(data, symbol)
        if rules.status != "Trading":
            raise DemoSafetyError(f"{symbol.value} instrument is not Trading")
        if not (rules.min_leverage <= Decimal("1") <= rules.max_leverage):
            raise DemoSafetyError("instrument does not support leverage 1")
        return rules

    def get_positions(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]:
        return self._paginate_list(
            "/v5/position/list",
            self._linear_scope(symbol, settle_coin),
            identity_fields=("symbol", "positionIdx"),
        )

    def get_open_orders(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]:
        params = self._linear_scope(symbol, settle_coin)
        params["openOnly"] = "0"
        return self._paginate_list(
            "/v5/order/realtime", params, identity_fields=("orderId",)
        )

    def get_order_history(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]:
        return self._paginate_list(
            "/v5/order/history",
            self._linear_scope(symbol, settle_coin),
            identity_fields=("orderId",),
        )

    def get_executions(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]:
        return self._paginate_list(
            "/v5/execution/list",
            self._linear_scope(symbol, settle_coin),
            identity_fields=("execId",),
        )

    def get_closed_pnl(
        self, symbol: Symbol | str | None = None, settle_coin: str | None = None
    ) -> list[dict[str, Any]]:
        return self._paginate_list(
            "/v5/position/closed-pnl",
            self._linear_scope(symbol, settle_coin),
            identity_fields=("orderId",),
        )

    def set_leverage(self, symbol: Symbol, leverage: Decimal) -> dict[str, Any]:
        value = _format_decimal(leverage)
        return self._request(
            "POST", "/v5/position/set-leverage",
            {"category": "linear", "symbol": symbol.value,
             "buyLeverage": value, "sellLeverage": value},
            allowed_ret_codes={110043},
        )

    def create_order(self, payload: dict[str, str]) -> dict[str, Any]:
        required = {"category", "symbol", "side", "orderType", "qty", "orderLinkId"}
        if not required.issubset(payload):
            raise ValueError("Demo order payload is incomplete")
        return self._request("POST", "/v5/order/create", payload)

    def cancel_order(self, symbol: Symbol, order_id: str) -> dict[str, Any]:
        return self._request(
            "POST", "/v5/order/cancel",
            {"category": "linear", "symbol": symbol.value, "orderId": order_id},
        )

    def set_trading_stop(
        self, symbol: Symbol, take_profit: Decimal, stop_loss: Decimal
    ) -> dict[str, Any]:
        return self._request(
            "POST", "/v5/position/trading-stop",
            {"category": "linear", "symbol": symbol.value,
             "takeProfit": _format_decimal(take_profit),
             "stopLoss": _format_decimal(stop_loss), "positionIdx": "0",
             "tpslMode": "Full", "tpOrderType": "Market",
             "slOrderType": "Market"},
        )

    @staticmethod
    def _linear_scope(
        symbol: Symbol | str | None, settle_coin: str | None
    ) -> dict[str, str]:
        if symbol is None and not settle_coin:
            raise DemoSafetyError("linear list request requires symbol or settleCoin")
        params = {"category": "linear"}
        if symbol is not None:
            value = symbol.value if isinstance(symbol, Symbol) else str(symbol)
            value = value.strip().upper()
            if not value:
                raise DemoSafetyError("linear list request symbol is empty")
            params["symbol"] = value
        else:
            coin = str(settle_coin).strip().upper()
            if coin != "USDT":
                raise DemoSafetyError("Demo account-wide linear scope must be USDT")
            params["settleCoin"] = coin
        return params

    def _paginate_list(
        self,
        path: str,
        params: dict[str, str],
        *,
        identity_fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        """Read all V5 pages, preserving scope and stopping cursor loops."""
        original = dict(params)
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_items: set[tuple[str, ...] | str] = set()
        collected: list[dict[str, Any]] = []
        while True:
            page_params = dict(original)
            if cursor:
                page_params["cursor"] = cursor
            data = self._request("GET", path, page_params)
            result = data.get("result") or {}
            items = result.get("list") if isinstance(result, dict) else None
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                identity = tuple(str(item.get(field) or "") for field in identity_fields)
                key: tuple[str, ...] | str = (
                    identity
                    if any(identity)
                    else json.dumps(item, sort_keys=True, separators=(",", ":"), default=str)
                )
                if key in seen_items:
                    continue
                seen_items.add(key)
                collected.append(item)
            next_cursor = (
                str(result.get("nextPageCursor") or "")
                if isinstance(result, dict) else ""
            )
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return collected

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        *,
        allowed_ret_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/v5/"):
            raise DemoSafetyError("Only Bybit V5 endpoints are allowed")
        timestamp = str(int(time.time() * 1000))
        recv_window = str(self.recv_window_ms)
        if method == "GET":
            body_text = urlencode(sorted(params.items()))
            url = f"{self.base_url}{path}?{body_text}"
            body = None
        else:
            body_text = json.dumps(params, separators=(",", ":"), sort_keys=True)
            url = f"{self.base_url}{path}"
            body = body_text.encode("utf-8")
        sign_payload = f"{timestamp}{self.api_key}{recv_window}{body_text}"
        signature = hmac.new(
            self._api_secret.encode(), sign_payload.encode(), hashlib.sha256
        ).hexdigest()
        headers = {
            "User-Agent": "ByBot/1.0 DEMO_ONLY",
            "Content-Type": "application/json",
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }
        data = self._http_request(method, url, headers, body, self.timeout_seconds)
        ret_code = int(data.get("retCode") or 0)
        if ret_code != 0 and ret_code not in (allowed_ret_codes or set()):
            ret_msg = str(data.get("retMsg") or "unknown error")
            raise DemoExchangeError(
                f"Bybit Demo request failed: {ret_msg}",
                ret_code=ret_code,
                ret_msg=ret_msg,
            )
        return data


class BybitDemoPrivateWebSocket:
    def __init__(self, client: BybitDemoRestClient) -> None:
        self.client = client
        self.reconnects = 0

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        url = f"{self.client.private_ws_url}/v5/private"
        while True:
            expires = int((time.time() + 10) * 1000)
            signature = hmac.new(
                self.client._api_secret.encode(),
                f"GET/realtime{expires}".encode(),
                hashlib.sha256,
            ).hexdigest()
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10, open_timeout=10
                ) as socket:
                    await socket.send(json.dumps({
                        "op": "auth", "args": [self.client.api_key, expires, signature]
                    }))
                    auth = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
                    if not auth.get("success"):
                        raise DemoExchangeError("Demo private WebSocket authentication failed")
                    await socket.send(json.dumps({
                        "op": "subscribe",
                        "args": ["order", "execution", "position", "wallet"],
                    }))
                    async for message in socket:
                        event = json.loads(message)
                        if isinstance(event, dict) and event.get("topic"):
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception:
                self.reconnects += 1
                await asyncio.sleep(min(30, 2 ** min(self.reconnects, 4)))


class DemoExecutionService:
    def __init__(
        self,
        settings: Settings,
        repository: Any,
        client: DemoExchangeClient | None,
        *,
        run_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.client = client
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_started_at = settings.demo_run_started_at or datetime.now(timezone.utc)
        run_digest = hashlib.sha256(self.run_id.encode("utf-8")).hexdigest()[:6]
        self.order_prefix = f"{settings.demo_order_link_prefix[:10]}-{run_digest}"
        self.enabled = bool(
            settings.execution_mode == ExecutionMode.BYBIT_DEMO
            and settings.bybit_demo_trading_enabled
            and settings.demo_order_execution_authorized
        )
        self.kill_switch_active = False
        self.kill_switch_reasons: list[str] = []
        self.last_error: str | None = None
        self.websocket_connected = False
        self.websocket_reconnects = 0
        self.reconciliation_incidents = 0
        self.orders_submitted = 0
        self.orders_accepted = 0
        self.orders_rejected = 0
        self.partial_fills = 0
        self.complete_fills = 0
        self.bot_owned_open_orders = 0
        self.unrelated_open_orders = 0
        self.bot_owned_open_positions = 0
        self.account_verified = False
        self.account_verified_at: datetime | None = None
        self.symbol_leverage: dict[str, dict[str, str]] = {}
        self.symbol_open_order_counts: dict[str, int] = {}
        self.usdt_order_reconciliation_ok = False
        self.usdt_position_reconciliation_ok = False
        self.leverage_normalized = False
        self._fee_rate_cache: dict[Symbol, tuple[Decimal, Decimal, datetime]] = {}

        self.account_margin_mode: str | None = None
        self.last_reconciliation_at: datetime | None = None
        self.reconciliation_in_progress = False
        self._last_reconcile_monotonic: float | None = None
        self._discard_ws_before_ms: int | None = None
        self.sleep_resume_reconciliations = 0
        self._submit_started_monotonic: dict[UUID, float] = {}
        self._ack_received_monotonic: dict[UUID, float] = {}
        self._execution_locks_guard = RLock()
        self._execution_locks: dict[UUID, RLock] = {}
        self.terminalization_retry_warnings = 0
        self.last_terminalization_warning: str | None = None
        self.terminalization_hard_failures: dict[str, list[str]] = {}
        if self.enabled:
            require_demo_execution(settings)
            if client is None:
                raise DemoSafetyError("Demo exchange client is unavailable")
            begin_run = getattr(self.repository, "begin_demo_soak_run", None)
            if callable(begin_run):
                persisted_run = begin_run(self.run_id, self.run_started_at)
                if persisted_run is None:
                    raise DemoSafetyError("Demo run boundary could not be persisted")
                self.run_started_at = persisted_run["started_at"]
        self.restore()

    def estimated_round_trip_fee_bps(self, symbol: Symbol) -> Decimal:
        """Read the authenticated fee tier; fail safely to configured taker cost."""
        current = datetime.now(timezone.utc)
        cached = self._fee_rate_cache.get(symbol)
        if cached and current - cached[2] <= timedelta(minutes=15):
            return cached[1] * Decimal("2")
        loader = getattr(self.client, "get_fee_rate", None)
        if not callable(loader):
            return self.settings.v2_taker_fee_bps * Decimal("2")
        try:
            maker, taker = loader(symbol)
        except Exception:
            return self.settings.v2_taker_fee_bps * Decimal("2")
        self._fee_rate_cache[symbol] = (maker, taker, current)
        return taker * Decimal("2")

    def restore(self) -> None:
        loader = getattr(self.repository, "load_demo_kill_switch", None)
        if callable(loader):
            state = loader()
            if state:
                self.kill_switch_active = bool(state["active"])
                self.kill_switch_reasons = list(state["reasons"])

    def verify_account_and_environment(self) -> bool:
        if not self.enabled or self.client is None:
            return False
        self.leverage_normalized = False
        self.symbol_leverage = {}
        self.symbol_open_order_counts = {}
        self.usdt_order_reconciliation_ok = False
        self.usdt_position_reconciliation_ok = False
        require_demo_execution(self.settings)
        validate_demo_domains(self.client.base_url, self.client.private_ws_url)
        self.account_verified = self.client.verify_credentials()
        if not self.account_verified:
            raise DemoSafetyError("Demo API credentials could not be verified")
        self.account_verified_at = datetime.now(timezone.utc)
        account_info_loader = getattr(self.client, "get_account_info", None)
        if callable(account_info_loader):
            account_info = account_info_loader()
            self.account_margin_mode = _sanitized_margin_mode(
                account_info.get("marginMode")
                or account_info.get("unifiedMarginStatus")
                or "unknown"
            )
        local = self.repository.load_demo_executions()
        active_local_symbols = {
            item.symbol.value for item in local
            if item.run_id == self.run_id
            if item.state not in TERMINAL_DEMO_STATES
        }
        local_order_links = {
            link
            for item in local
            for link in (item.order_link_id, item.close_order_link_id)
            if link
        }
        owned_protection_order_ids: set[str] = set()
        for symbol_value in self.settings.allowed_symbols:
            symbol = Symbol(symbol_value)
            self.client.get_instrument(symbol)
            positions = self.client.get_positions(symbol)
            open_orders = self.client.get_open_orders(symbol=symbol)
            self.symbol_open_order_counts[symbol.value] = len(open_orders)
            conflicts = [
                order for order in open_orders
                if str(order.get("orderLinkId") or "") not in local_order_links
                and not any(
                    _is_owned_bybit_protection_order(order, execution, positions)
                    for execution in local
                    if execution.symbol == symbol
                    and execution.state == DemoExecutionState.DEMO_POSITION_OPEN
                )
            ]
            for order in open_orders:
                if any(
                    _is_owned_bybit_protection_order(order, execution, positions)
                    for execution in local
                    if execution.symbol == symbol
                    and execution.state == DemoExecutionState.DEMO_POSITION_OPEN
                ):
                    order_id = str(order.get("orderId") or "")
                    if order_id:
                        owned_protection_order_ids.add(order_id)
            if conflicts:
                self._activate_kill_switch(
                    f"unattributed active Demo order for {symbol.value}"
                )
                raise DemoSafetyError(
                    f"unrelated active Demo order conflicts with {symbol.value} preflight"
                )
            for position in positions:
                if int(position.get("positionIdx") or 0) != 0:
                    raise DemoSafetyError("hedge position mode is not supported")
            self._ensure_symbol_leverage(
                symbol, positions=positions, open_orders=open_orders,
                desired_leverage=(
                    self.settings.v2_leverage_for_symbol(symbol.value)
                    if self.settings.v2_enabled else Decimal("1")
                ),
            )
            for position in positions:
                if (
                    _decimal(position.get("size"), default="0") > 0
                    and symbol.value not in active_local_symbols
                ):
                    self._activate_kill_switch("unattributed remote Demo position")
                    raise DemoSafetyError("unrelated open Demo position conflicts with preflight")
        # Bybit V5 linear list endpoints must always be scoped. Account-wide
        # reconciliation deliberately uses USDT, while symbol checks above stay
        # symbol-scoped so their diagnostics remain actionable.
        usdt_orders = self.client.get_open_orders(settle_coin="USDT")
        self.usdt_order_reconciliation_ok = True
        if any(
            str(order.get("orderLinkId") or "") not in local_order_links
            and str(order.get("orderId") or "") not in owned_protection_order_ids
            for order in usdt_orders
        ):
            self._activate_kill_switch("unattributed active USDT Demo order")
            raise DemoSafetyError("unrelated active USDT Demo order conflicts with preflight")
        usdt_positions = self.client.get_positions(settle_coin="USDT")
        self.usdt_position_reconciliation_ok = True
        if any(
            _decimal(position.get("size"), default="0") > 0
            and str(position.get("symbol") or "") not in active_local_symbols
            for position in usdt_positions
        ):
            self._activate_kill_switch("unattributed remote Demo position")
            raise DemoSafetyError("unrelated open USDT Demo position conflicts with preflight")
        self.leverage_normalized = True
        return self.account_verified

    def _verify_execution_environment_cached(self) -> None:
        """Refresh only authenticated identity when the startup proof is stale."""
        if self.client is None:
            raise DemoSafetyError("Demo client unavailable")
        require_demo_execution(self.settings)
        validate_demo_domains(self.client.base_url, self.client.private_ws_url)
        now = datetime.now(timezone.utc)
        ttl = timedelta(seconds=self.settings.v2_demo_account_verification_ttl_seconds)
        if (
            self.account_verified
            and self.account_verified_at is not None
            and now - self.account_verified_at <= ttl
        ):
            return
        if not self.client.verify_credentials():
            self.account_verified = False
            raise DemoSafetyError("Demo API credentials could not be verified")
        self.account_verified = True
        self.account_verified_at = now

    def _ensure_symbol_leverage(
        self,
        symbol: Symbol,
        *,
        positions: list[dict[str, Any]] | None = None,
        open_orders: list[dict[str, Any]] | None = None,
        desired_leverage: Decimal = Decimal("1"),
        rules: InstrumentRules | None = None,
        allow_mutation: bool = True,
    ) -> list[dict[str, Any]]:
        """Confirm target leverage, normalizing only a completely flat Demo symbol."""
        if self.client is None:
            raise DemoSafetyError("Demo client unavailable")
        require_demo_execution(self.settings)
        validate_demo_domains(self.client.base_url, self.client.private_ws_url)
        current_positions = (
            positions if positions is not None else self.client.get_positions(symbol)
        )
        buy_leverage, sell_leverage = _position_leverages(current_positions, symbol)
        self.symbol_leverage[symbol.value] = {
            "buy": _format_decimal(buy_leverage),
            "sell": _format_decimal(sell_leverage),
        }
        current_rules = rules or self.client.get_instrument(symbol)
        target = min(
            max(desired_leverage, current_rules.min_leverage),
            current_rules.max_leverage,
        )
        steps = ((target - current_rules.min_leverage) / current_rules.leverage_step).to_integral_value(
            rounding=ROUND_DOWN
        )
        target = current_rules.min_leverage + steps * current_rules.leverage_step
        if buy_leverage == target and sell_leverage == target:
            return current_positions
        if not allow_mutation:
            raise DemoSafetyError(
                f"{symbol.value} leverage is not preconfigured to the required value"
            )
        if any(_decimal(item.get("size"), default="0") > 0 for item in current_positions):
            raise DemoSafetyError(
                f"{symbol.value} leverage is not 1x and an open position prevents normalization"
            )
        active_orders = (
            open_orders
            if open_orders is not None
            else self.client.get_open_orders(symbol=symbol)
        )
        if active_orders:
            raise DemoSafetyError(
                f"{symbol.value} leverage is not 1x and active orders prevent normalization"
            )
        try:
            self.client.set_leverage(symbol, target)
            confirmed_positions = self.client.get_positions(symbol)
            confirmed_buy, confirmed_sell = _position_leverages(confirmed_positions, symbol)
        except Exception as exc:
            mode = self.account_margin_mode or "unknown"
            raise DemoSafetyError(
                f"{symbol.value} leverage normalization failed "
                f"(buy={_format_decimal(buy_leverage)}, "
                f"sell={_format_decimal(sell_leverage)}, margin_mode={mode})"
            ) from exc
        self.symbol_leverage[symbol.value] = {
            "buy": _format_decimal(confirmed_buy),
            "sell": _format_decimal(confirmed_sell),
        }
        if confirmed_buy != target or confirmed_sell != target:
            mode = self.account_margin_mode or "unknown"
            raise DemoSafetyError(
                f"{symbol.value} leverage could not be confirmed as 1x "
                f"(buy={_format_decimal(confirmed_buy)}, "
                f"sell={_format_decimal(confirmed_sell)}, margin_mode={mode})"
            )
        return confirmed_positions

    def submit_candidate(
        self,
        candidate: NewsSignalCandidate,
        preview: SignalRiskPreview,
        classification: NewsClassification,
        snapshot: MarketSnapshot,
        *,
        canary_plan: CanaryMinimumOrderPlan | None = None,
        desired_leverage: Decimal | None = None,
        strategy_name: str | None = None,
        strategy_version: str | None = None,
        trailing_stop_pct: Decimal | None = None,
        break_even_at_r: Decimal | None = None,
        maximum_holding_seconds: int | None = None,
        latency_timeline: dict[str, datetime | None] | None = None,
        instrument_rules: InstrumentRules | None = None,
        pre_submit_market_guard: Callable[
            [], Decimal | tuple[Decimal, Decimal]
        ] | None = None,
        sizing_details: dict[str, Any] | None = None,
    ) -> DemoExecutionRecord | None:
        if not self.enabled or self.client is None:
            return None
        require_demo_execution(self.settings)
        if candidate.execution_environment != ExecutionEnvironment.BYBIT_DEMO:
            self.last_error = "candidate execution environment is not BYBIT_DEMO"
            return None
        if self.kill_switch_active:
            self.last_error = "Demo execution kill switch is active"
            return None
        if candidate.state.value != "READY" or not preview.approved:
            self.last_error = "candidate or risk preview is not executable"
            return None
        if preview.risk_decision_id is None:
            self.last_error = "approved risk decision is not durably persisted"
            return None
        if not classification.trade_eligible:
            self.last_error = "classification is not trade eligible"
            return None
        if candidate.symbol is None or candidate.final_action == NewsSignalAction.NO_TRADE:
            self.last_error = "candidate has no executable direction"
            return None

        timeline: dict[str, Any] = dict(latency_timeline or {})
        durations: dict[str, float] = {}

        def start(stage: str) -> float:
            timeline[f"{stage}_started_at"] = datetime.now(timezone.utc)
            return perf_counter()

        def complete(stage: str, started: float) -> None:
            timeline[f"{stage}_completed_at"] = datetime.now(timezone.utc)
            durations[stage] = (perf_counter() - started) * 1000

        timeline.setdefault("execution_task_received_at", datetime.now(timezone.utc))
        ownership_started = start("ownership_check")
        existing = self.repository.get_demo_execution(str(candidate.id))
        complete("ownership_check", ownership_started)
        if existing is not None:
            return existing

        entry_reference = Decimal(str(
            snapshot.ask_price
            if candidate.final_action == NewsSignalAction.BUY else snapshot.bid_price
        ))
        account_started = start("account_verification")
        self._verify_execution_environment_cached()
        complete("account_verification", account_started)

        instrument_started = start("instrument_metadata")
        # The validated universe owns immutable normal-strategy rules. Canary
        # execution deliberately keeps its final just-in-time exchange refresh.
        rules = (
            self.client.get_instrument(candidate.symbol)
            if canary_plan is not None or instrument_rules is None
            else instrument_rules
        )
        complete("instrument_metadata", instrument_started)

        quantity_started = start("quantity_normalization")
        if canary_plan is not None:
            # This is the final exchange-rules read before the durable reservation.
            # Recalculate at the latest executable reference price and submit only
            # the exchange-minimum valid quantity, never the whole budget.
            final_plan = revalidate_canary_order_plan(
                canary_plan, rules, entry_reference
            )
            quantity = final_plan.calculated_order_qty
        else:
            quantity = normalize_quantity(Decimal(str(preview.capped_size)), rules)
        validate_order_notional(quantity, entry_reference, rules)
        complete("quantity_normalization", quantity_started)

        protection_started = start("protection_plan")
        if candidate.proposed_stop_loss_pct <= 0 or candidate.proposed_take_profit_pct <= 0:
            raise DemoSafetyError("protection plan requires positive TP and SL")
        complete("protection_plan", protection_started)

        def measured_read(stage: str, loader: Callable[[], Any]) -> tuple[Any, datetime, datetime, float]:
            started_at = datetime.now(timezone.utc)
            started_perf = perf_counter()
            value = loader()
            return (
                value,
                started_at,
                datetime.now(timezone.utc),
                (perf_counter() - started_perf) * 1000,
            )

        # Independent signed GETs run concurrently. They retain every remote
        # conflict/loss check while avoiding three sequential position reads.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="demo-entry-read") as pool:
            reads = {
                "position_symbol": pool.submit(
                    measured_read,
                    "position_symbol",
                    lambda: self.client.get_positions(symbol=candidate.symbol),
                ),
                "open_orders": pool.submit(
                    measured_read,
                    "open_orders",
                    lambda: self.client.get_open_orders(symbol=candidate.symbol),
                ),
                "position_account": pool.submit(
                    measured_read,
                    "position_account",
                    lambda: self.client.get_positions(settle_coin="USDT"),
                ),
                "reconciliation_check": pool.submit(
                    measured_read,
                    "reconciliation_check",
                    lambda: self.client.get_closed_pnl(settle_coin="USDT"),
                ),
            }
            read_results = {name: future.result() for name, future in reads.items()}

        symbol_positions, symbol_started, symbol_completed, symbol_ms = read_results["position_symbol"]
        account_positions, account_pos_started, account_pos_completed, account_pos_ms = read_results["position_account"]
        open_orders, orders_started, orders_completed, orders_ms = read_results["open_orders"]
        closed_pnl, reconcile_started, reconcile_completed, reconcile_ms = read_results["reconciliation_check"]
        timeline["position_query_started_at"] = min(symbol_started, account_pos_started)
        timeline["position_query_completed_at"] = max(symbol_completed, account_pos_completed)
        durations["position_query"] = max(symbol_ms, account_pos_ms)
        timeline["open_orders_query_started_at"] = orders_started
        timeline["open_orders_query_completed_at"] = orders_completed
        durations["open_orders_query"] = orders_ms
        timeline["reconciliation_check_started_at"] = reconcile_started
        timeline["reconciliation_check_completed_at"] = reconcile_completed
        durations["reconciliation_check"] = reconcile_ms

        self._enforce_risk_controls(candidate.symbol, closed_pnl=closed_pnl)
        self._validate_remote_entry_state(
            candidate.symbol,
            positions=symbol_positions,
            open_orders=open_orders,
            all_positions=account_positions,
        )
        leverage = desired_leverage or Decimal(str(self.settings.demo_leverage))
        leverage_started = start("leverage_setup")
        verified_positions = self._ensure_symbol_leverage(
            candidate.symbol,
            positions=symbol_positions,
            open_orders=open_orders,
            desired_leverage=leverage,
            rules=rules,
            allow_mutation=False,
        )
        self._verify_leverage_and_mode(
            candidate.symbol,
            desired_leverage=leverage,
            positions=verified_positions,
        )
        complete("leverage_setup", leverage_started)
        side = Side.BUY if candidate.final_action == NewsSignalAction.BUY else Side.SELL
        order_link_id = deterministic_order_link_id(
            self.order_prefix, candidate.id, "entry"
        )
        record = DemoExecutionRecord(
            candidate_id=candidate.id,
            risk_decision_id=preview.risk_decision_id,
            run_id=self.run_id,
            order_link_id=order_link_id,
            state=DemoExecutionState.DEMO_SUBMITTING,
            symbol=candidate.symbol,
            side=side,
            requested_quantity=quantity,
            reference_entry_price=entry_reference,
            leverage=leverage,
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            sizing_details=dict(sizing_details or {}),
            stop_loss_pct=Decimal(str(candidate.proposed_stop_loss_pct)),
            take_profit_pct=Decimal(str(candidate.proposed_take_profit_pct)),
            trailing_stop_pct=trailing_stop_pct,
            break_even_at_r=break_even_at_r,
            maximum_holding_seconds=maximum_holding_seconds,
            signal_created_at=candidate.created_at,
            candidate_persisted_at=(latency_timeline or {}).get("candidate_persisted_at"),
            reservation_requested_at=(latency_timeline or {}).get("reservation_requested_at"),
            reservation_created_at=(latency_timeline or {}).get("reservation_created_at"),
            risk_evaluation_started_at=(latency_timeline or {}).get("risk_evaluation_started_at"),
            risk_approved_at=(latency_timeline or {}).get("risk_approved_at"),
            execution_dispatched_at=(latency_timeline or {}).get("execution_dispatched_at"),
            execution_task_received_at=timeline.get("execution_task_received_at"),
            ownership_check_started_at=timeline.get("ownership_check_started_at"),
            ownership_check_completed_at=timeline.get("ownership_check_completed_at"),
            reconciliation_check_started_at=timeline.get("reconciliation_check_started_at"),
            reconciliation_check_completed_at=timeline.get("reconciliation_check_completed_at"),
            account_verification_started_at=timeline.get("account_verification_started_at"),
            account_verification_completed_at=timeline.get("account_verification_completed_at"),
            position_query_started_at=timeline.get("position_query_started_at"),
            position_query_completed_at=timeline.get("position_query_completed_at"),
            open_orders_query_started_at=timeline.get("open_orders_query_started_at"),
            open_orders_query_completed_at=timeline.get("open_orders_query_completed_at"),
            instrument_metadata_started_at=timeline.get("instrument_metadata_started_at"),
            instrument_metadata_completed_at=timeline.get("instrument_metadata_completed_at"),
            leverage_setup_started_at=timeline.get("leverage_setup_started_at"),
            leverage_setup_completed_at=timeline.get("leverage_setup_completed_at"),
            quantity_normalization_started_at=timeline.get("quantity_normalization_started_at"),
            quantity_normalization_completed_at=timeline.get("quantity_normalization_completed_at"),
            protection_plan_started_at=timeline.get("protection_plan_started_at"),
            protection_plan_completed_at=timeline.get("protection_plan_completed_at"),
            execution_stage_durations_ms=durations,
        )
        if pre_submit_market_guard is not None:
            guard_result = pre_submit_market_guard()
            if isinstance(guard_result, tuple):
                entry_reference, quantity = guard_result
                entry_reference = Decimal(str(entry_reference))
                quantity = Decimal(str(quantity))
                record.requested_quantity = quantity
                if record.sizing_details:
                    record.sizing_details[
                        "normalized_accepted_quantity"
                    ] = str(quantity)
                    record.sizing_details[
                        "normalized_accepted_notional_usdt"
                    ] = str(quantity * entry_reference)
            else:
                entry_reference = Decimal(str(guard_result))
            validate_order_notional(quantity, entry_reference, rules)
            record.reference_entry_price = entry_reference
        database_started = start("database_execution_state")
        record.database_execution_state_started_at = timeline["database_execution_state_started_at"]
        reserved = self.repository.reserve_demo_execution(record)
        complete("database_execution_state", database_started)
        record.database_execution_state_completed_at = timeline["database_execution_state_completed_at"]
        record.execution_stage_durations_ms.update(durations)
        if reserved is None:
            self.last_error = "durable Demo execution reservation failed"
            return None
        if reserved is not None and reserved.id != record.id:
            return reserved
        self.repository.save_demo_execution(
            record, event_type="DEMO_EXECUTION_PREFLIGHT_COMPLETE"
        )
        try:
            record.exchange_submit_started_at = datetime.now(timezone.utc)
            record.order_submitted_at = record.exchange_submit_started_at
            record.local_submit_started_at = record.exchange_submit_started_at
            submit_started_perf = perf_counter()
            self._submit_started_monotonic[record.id] = submit_started_perf
            response = self.client.create_order({
                "category": "linear",
                "symbol": candidate.symbol.value,
                "side": "Buy" if side == Side.BUY else "Sell",
                "orderType": "Market",
                "qty": _format_decimal(quantity),
                "timeInForce": "IOC",
                "positionIdx": "0",
                "orderLinkId": order_link_id,
            })
            result = response.get("result") or {}
            record.order_id = str(result.get("orderId") or "") or None
            record.local_ack_received_at = datetime.now(timezone.utc)
            record.order_acknowledged_at = record.local_ack_received_at
            ack_perf = perf_counter()
            self._ack_received_monotonic[record.id] = ack_perf
            record.execution_stage_durations_ms["exchange_submit"] = (
                ack_perf - submit_started_perf
            ) * 1000
            if response.get("time") is not None:
                record.exchange_order_created_at = _timestamp(response.get("time"))
            record.state = DemoExecutionState.DEMO_ORDER_ACKNOWLEDGED
            record.exchange_order_status = "Acknowledged"
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(
                record, event_type="DEMO_ORDER_ACKNOWLEDGED"
            )
            self.orders_submitted += 1
            self.orders_accepted += 1
        except Exception as exc:
            record.state = DemoExecutionState.DEMO_RECONCILIATION_REQUIRED
            record.last_error = _sanitized_error(exc)
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(record, event_type="CREATE_UNCERTAIN")
            self.orders_rejected += 1
            self.last_error = record.last_error
            self._activate_kill_switch("entry submission outcome is uncertain")
            return record
        # Market orders can fill before the first private WebSocket event.
        # Query every authoritative REST surface immediately, by the stable
        # order identifiers, so the durable state never depends on WS timing.
        try:
            return self._reconcile_execution_rest(record)
        except Exception as exc:
            record.last_error = _sanitized_error(exc)
            record.last_reconciliation_at = datetime.now(timezone.utc)
            record.updated_at = record.last_reconciliation_at
            self.repository.save_demo_execution(
                record, event_type="REST_RECONCILIATION_PENDING"
            )
            self.last_error = record.last_error
            return record

    def validate_canary_notional(
        self, symbol: Symbol, notional_usdt: Decimal, reference_price: Decimal
    ) -> Decimal:
        """Fail before persistence when the exchange minimum exceeds the canary cap."""
        if not self.enabled or self.client is None or not self.settings.demo_canary_enabled:
            raise DemoSafetyError("Demo canary execution is unavailable")
        require_demo_execution(self.settings)
        plan = self.plan_canary_order(symbol, notional_usdt, reference_price)
        return plan.calculated_order_qty

    def plan_canary_order(
        self,
        symbol: Symbol,
        max_notional_usdt: Decimal,
        reference_price: Decimal,
        *,
        safety_buffer_pct: Decimal | None = None,
    ) -> CanaryMinimumOrderPlan:
        """Read current instrument rules and build a side-effect-free plan."""

        if not self.enabled or self.client is None or not self.settings.demo_canary_enabled:
            raise DemoSafetyError("Demo canary execution is unavailable")
        require_demo_execution(self.settings)
        rules = self.client.get_instrument(symbol)
        return calculate_minimum_valid_canary_order(
            rules,
            reference_price,
            max_notional_usdt,
            safety_buffer_pct=(
                safety_buffer_pct
                if safety_buffer_pct is not None
                else self.settings.demo_canary_market_price_buffer_pct
            ),
        )

    def handle_private_event(self, event: dict[str, Any]) -> None:
        topic = str(event.get("topic") or "")
        for item in event.get("data") or []:
            if not isinstance(item, dict):
                continue
            event_ms = _event_timestamp_ms(event, item)
            if self._discard_ws_before_ms is not None and (
                event_ms is None or event_ms <= self._discard_ws_before_ms
            ):
                # A private stream can replay buffered messages after Windows
                # resumes.  The REST watermark is authoritative; never let an
                # older event regress a reconciled execution.
                continue
            event_key = _event_key(topic, item)
            if not self.repository.record_demo_event(event_key, topic, item):
                continue
            order_link_id = str(item.get("orderLinkId") or "")
            order_id = str(item.get("orderId") or "")
            record = self.repository.find_demo_execution(order_link_id, order_id)
            attributed_close = False
            if record is None:
                if order_link_id.startswith(f"{self.settings.demo_order_link_prefix}-"):
                    self._activate_kill_switch("unknown bot-created remote order")
                    continue
                if topic == "position":
                    matching = [
                        entry for entry in self.repository.load_demo_executions()
                        if entry.symbol.value == str(item.get("symbol") or "")
                        and entry.state not in {
                            DemoExecutionState.DEMO_CLOSED,
                            DemoExecutionState.DEMO_FAILED,
                        }
                    ]
                    record = matching[0] if len(matching) == 1 else None
                else:
                    record = self._attributable_close_record(item)
                if record is None:
                    continue
                attributed_close = True
            with self._execution_lock(record.id):
                if topic == "execution":
                    self._apply_fill(record, item, force_close=attributed_close)
                elif topic == "order":
                    self._apply_order_update(record, item, force_close=attributed_close)
                elif topic == "position":
                    self._apply_position_update(record, item)
                if self._ws_close_requires_terminal_reconciliation(
                    topic, item, record
                ):
                    try:
                        self._reconcile_execution_rest(record)
                    except DemoExchangeError as exc:
                        self._mark_terminalization_retry(record, exc)

    def _reconcile_execution_rest(
        self, record: DemoExecutionRecord
    ) -> DemoExecutionRecord:
        with self._execution_lock(record.id):
            loader = getattr(self.repository, "get_demo_execution", None)
            latest = loader(str(record.candidate_id)) if callable(loader) else None
            if latest is not None:
                if latest.state in TERMINAL_DEMO_STATES:
                    return latest
                if _aware(latest.updated_at) >= _aware(record.updated_at):
                    record = latest
            return self._reconcile_execution_rest_locked(record)

    def _reconcile_execution_rest_locked(
        self, record: DemoExecutionRecord
    ) -> DemoExecutionRecord:
        """Reconcile one execution from all authoritative symbol-scoped REST data."""
        if self.client is None:
            return record
        starting_fingerprint = _execution_material_fingerprint(record)
        realtime = self.client.get_open_orders(symbol=record.symbol)
        history = self.client.get_order_history(symbol=record.symbol)
        executions = self.client.get_executions(symbol=record.symbol)
        positions = self.client.get_positions(symbol=record.symbol)
        _capture_protection_order_ownership(record, realtime, positions)

        def matches(item: dict[str, Any], *, close: bool = False) -> bool:
            ids = (
                (record.close_order_id, record.close_order_link_id)
                if close else (record.order_id, record.order_link_id)
            )
            return bool(
                (ids[0] and str(item.get("orderId") or "") == ids[0])
                or (ids[1] and str(item.get("orderLinkId") or "") == ids[1])
            )

        for order in [*realtime, *history]:
            if matches(order) or matches(order, close=True):
                self._apply_order_update(record, order)
        for execution in executions:
            if matches(execution):
                self._apply_fill(record, execution)
            elif matches(execution, close=True):
                self._apply_fill(record, execution, force_close=True)

        position = next(
            (
                item for item in positions
                if str(item.get("symbol") or "") == record.symbol.value
                and _decimal(item.get("size"), default="0") > 0
            ),
            None,
        )
        close_started = bool(
            record.close_order_id or record.close_order_link_id or record.close_fills
        )
        if (
            position is not None
            and record.state not in TERMINAL_DEMO_STATES
            and not close_started
            and record.state != DemoExecutionState.DEMO_CLOSING
        ):
            remote_size = _decimal(position.get("size"), default="0")
            if record.accepted_quantity == 0:
                record.accepted_quantity = remote_size
            remote_average = _decimal(position.get("avgPrice"), default="0")
            if record.average_fill_price is None and remote_average > 0:
                record.average_fill_price = remote_average
            if (
                record.accepted_quantity >= record.requested_quantity
                and record.average_fill_price is not None
                and record.state in {
                    DemoExecutionState.DEMO_ORDER_ACKNOWLEDGED,
                    DemoExecutionState.DEMO_ACCEPTED,
                    DemoExecutionState.DEMO_PARTIALLY_FILLED,
                    DemoExecutionState.DEMO_FILLED,
                    DemoExecutionState.DEMO_FULLY_FILLED,
                }
            ):
                record.state = DemoExecutionState.DEMO_FULLY_FILLED
                record.exchange_order_status = "Filled"
                record.updated_at = datetime.now(timezone.utc)
                self.repository.save_demo_execution(
                    record, event_type="DEMO_FULLY_FILLED"
                )
                self._install_protection(record)
        finalized = self._finalize_attributed_flat_close(
            record,
            realtime=realtime,
            history=history,
            executions=executions,
            positions=positions,
        )
        if (
            not finalized
            and record.state == DemoExecutionState.DEMO_CLOSING
            and record.close_fills
            and record.closed_at is not None
            and not any(
                str(item.get("symbol") or "") == record.symbol.value
                and _decimal(item.get("size"), default="0") > 0
                for item in positions
            )
        ):
            self._record_terminalization_invariant(
                record, realtime=realtime, history=history,
                executions=executions, positions=positions,
            )
        record.last_reconciliation_at = datetime.now(timezone.utc)
        record.updated_at = record.last_reconciliation_at
        if not finalized and _execution_material_fingerprint(record) != starting_fingerprint:
            self.repository.save_demo_execution(record, event_type="REST_ORDER_RECONCILED")
        return record

    def _finalize_attributed_flat_close(
        self,
        record: DemoExecutionRecord,
        *,
        realtime: list[dict[str, Any]],
        history: list[dict[str, Any]],
        executions: list[dict[str, Any]],
        positions: list[dict[str, Any]],
    ) -> bool:
        """Finalize an owned position only from exact, complete REST evidence."""

        if record.state not in {
            DemoExecutionState.DEMO_POSITION_OPEN,
            DemoExecutionState.DEMO_CLOSING,
            DemoExecutionState.DEMO_RECONCILIATION_REQUIRED,
        }:
            return False
        if any(
            str(item.get("symbol") or "") == record.symbol.value
            and _decimal(item.get("size"), default="0") > 0
            for item in positions
        ):
            return False
        if any(_is_execution_owned_open_order(item, record) for item in realtime):
            return False
        close_order = next(
            (
                item for item in history
                if record.close_order_id
                and str(item.get("orderId") or "") == record.close_order_id
            ),
            None,
        )
        if close_order is None and record.accepted_quantity > 0:
            entry_time_ms = _record_entry_time_ms(record)
            expected_side = "SELL" if record.side == Side.BUY else "BUY"
            all_records = self.repository.load_demo_executions()
            candidates: list[dict[str, Any]] = []
            for item in history:
                order_id = str(item.get("orderId") or "")
                if (
                    not order_id
                    or str(item.get("symbol") or "") != record.symbol.value
                    or str(item.get("side") or "").upper() != expected_side
                    or str(item.get("orderStatus") or "") != "Filled"
                    or str(item.get("reduceOnly") or "").lower() != "true"
                    or _exchange_event_time_ms(item) < entry_time_ms
                    or _decimal(
                        item.get("cumExecQty") or item.get("qty"), default="0"
                    ) != record.accepted_quantity
                    or classify_exchange_close(item) is None
                    or _exchange_identity_used_by_other(
                        all_records, record, order_id, ""
                    )
                ):
                    continue
                candidates.append(item)
            if len(candidates) == 1:
                close_order = candidates[0]
        if close_order is None or str(close_order.get("orderStatus") or "") != "Filled":
            return False
        entry_time_ms = _record_entry_time_ms(record)
        selected_close_order_id = str(close_order.get("orderId") or "")
        expected_close_side = "SELL" if record.side == Side.BUY else "BUY"
        if (
            not selected_close_order_id
            or str(close_order.get("symbol") or "") != record.symbol.value
            or str(close_order.get("side") or "").upper() != expected_close_side
            or str(close_order.get("reduceOnly") or "").lower() != "true"
            or _exchange_event_time_ms(close_order) < entry_time_ms
            or _decimal(
                close_order.get("cumExecQty") or close_order.get("qty"), default="0"
            ) != record.accepted_quantity
            or classify_exchange_close(
                close_order,
                durable_reason=record.exit_attribution or record.close_reason,
            ) is None
        ):
            return False
        attributed = [
            item for item in executions
            if str(item.get("orderId") or "") == selected_close_order_id
            and _exchange_event_time_ms(item) >= entry_time_ms
            and (
                (record.side == Side.BUY and str(item.get("side") or "").upper() == "SELL")
                or (record.side == Side.SELL and str(item.get("side") or "").upper() == "BUY")
            )
        ]
        if not attributed:
            return False
        all_records = self.repository.load_demo_executions()
        if any(
            _exchange_identity_used_by_other(
                all_records,
                record,
                str(item.get("orderId") or ""),
                str(item.get("execId") or ""),
            )
            for item in attributed
        ):
            return False
        close_quantity = sum(
            (_decimal(item.get("execQty"), default="0") for item in attributed),
            Decimal("0"),
        )
        if record.accepted_quantity <= 0 or close_quantity != record.accepted_quantity:
            return False
        record.close_order_id = selected_close_order_id
        attribute_exchange_close(
            record, close_order, source="rest_exact_full_close_order"
        )
        # _apply_fill owns exact fill persistence and PnL/fee calculation. It
        # may already have processed these rows; either way the durable result
        # must contain the same complete quantity before terminalization.
        for item in attributed:
            self._apply_fill(record, item, force_close=True)
        durable_close_quantity = sum(
            (fill.quantity for fill in record.close_fills), Decimal("0")
        )
        if durable_close_quantity != record.accepted_quantity:
            return False
        attribution = canonical_exit_attribution(
            record.exit_attribution or record.close_reason
        )
        record.state = (
            DemoExecutionState.DEMO_CLOSED_EXTERNALLY
            if attribution == "manual_external_close"
            else DemoExecutionState.DEMO_CLOSED
            if attribution in {"take_profit", "stop_loss"}
            else DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE
            if record.failure_reason
            else DemoExecutionState.DEMO_CLOSED
        )
        record.cleanup_result = "remote position flat and bot-owned orders zero"
        record.last_error = None
        record.closed_at = max(fill.executed_at for fill in record.close_fills)
        record.updated_at = datetime.now(timezone.utc)
        terminalizer = getattr(
            self.repository, "terminalize_demo_execution", None
        )
        if callable(terminalizer):
            result = terminalizer(
                record, event_type="DEMO_CLOSE_TERMINALIZED"
            )
            if result == "FAILED":
                raise RuntimeError("durable Demo terminalization failed")
        else:
            self.repository.save_demo_execution(
                record, event_type="DEMO_CLOSE_TERMINALIZED"
            )
        return True

    def _finalize_durable_exact_flat_close(
        self,
        record: DemoExecutionRecord,
        *,
        realtime: list[dict[str, Any]],
        positions: list[dict[str, Any]],
    ) -> bool:
        """Finalize complete durable fills without consulting symbol-level PnL."""
        if (
            record.state not in {
                DemoExecutionState.DEMO_POSITION_OPEN,
                DemoExecutionState.DEMO_CLOSING,
                DemoExecutionState.DEMO_RECONCILIATION_REQUIRED,
            }
            or not record.order_id
            or not record.close_order_id
            or not record.fills
            or not record.close_fills
            or any(
                str(item.get("symbol") or "") == record.symbol.value
                and _decimal(item.get("size"), default="0") > 0
                for item in positions
            )
            or any(_is_execution_owned_open_order(item, record) for item in realtime)
        ):
            return False
        attribution = canonical_exit_attribution(
            record.exit_attribution or record.close_reason
        )
        if (
            attribution == "unattributed_external_close"
            and record.failure_reason
            and record.close_order_link_id
        ):
            # A durable bot-generated close link plus complete exact fills is
            # sufficient ownership evidence for the existing cleanup path.
            attribution = "forced_cleanup"
        if attribution == "unattributed_external_close":
            return False
        entry_time_ms = _record_entry_time_ms(record)
        if (
            any(fill.order_id != record.order_id for fill in record.fills)
            or any(fill.order_id != record.close_order_id for fill in record.close_fills)
            or any(
                int(fill.executed_at.timestamp() * 1000) < entry_time_ms
                for fill in record.close_fills
            )
        ):
            return False
        all_records = self.repository.load_demo_executions()
        if any(
            _exchange_identity_used_by_other(
                all_records, record, fill.order_id, fill.execution_id
            )
            for fill in [*record.fills, *record.close_fills]
        ):
            return False
        entry_qty = sum((fill.quantity for fill in record.fills), Decimal("0"))
        close_qty = sum((fill.quantity for fill in record.close_fills), Decimal("0"))
        if (
            entry_qty != record.accepted_quantity
            or close_qty != record.accepted_quantity
            or record.accepted_quantity <= 0
        ):
            return False
        entry_average = sum(
            (fill.quantity * fill.price for fill in record.fills), Decimal("0")
        ) / entry_qty
        close_average = sum(
            (fill.quantity * fill.price for fill in record.close_fills), Decimal("0")
        ) / close_qty
        fees = sum(
            (fill.fee for fill in [*record.fills, *record.close_fills]),
            Decimal("0"),
        )
        direction = Decimal("1") if record.side == Side.BUY else Decimal("-1")
        record.average_fill_price = entry_average
        record.average_close_price = close_average
        record.exchange_fees = fees
        record.gross_realized_pnl = (
            (close_average - entry_average) * close_qty * direction
        )
        record.paper_shadow_pnl = record.gross_realized_pnl - fees
        record.realized_exchange_pnl = record.paper_shadow_pnl
        record.exit_attribution = attribution
        record.close_reason = attribution
        record.closed_at = max(fill.executed_at for fill in record.close_fills)
        record.state = (
            DemoExecutionState.DEMO_CLOSED_EXTERNALLY
            if attribution == "manual_external_close"
            else DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE
            if record.failure_reason
            else DemoExecutionState.DEMO_CLOSED
        )
        record.cleanup_result = "remote position flat and bot-owned orders zero"
        record.last_error = None
        record.updated_at = datetime.now(timezone.utc)
        terminalizer = getattr(self.repository, "terminalize_demo_execution", None)
        if callable(terminalizer):
            result = terminalizer(
                record, event_type="DEMO_CLOSE_TERMINALIZED"
            )
            if result == "FAILED":
                raise RuntimeError("durable Demo terminalization failed")
        else:
            self.repository.save_demo_execution(
                record, event_type="DEMO_CLOSE_TERMINALIZED"
            )
        return True

    def _execution_lock(self, execution_id: UUID) -> RLock:
        with self._execution_locks_guard:
            return self._execution_locks.setdefault(execution_id, RLock())

    @staticmethod
    def _ws_close_requires_terminal_reconciliation(
        topic: str, item: dict[str, Any], record: DemoExecutionRecord,
    ) -> bool:
        if record.state != DemoExecutionState.DEMO_CLOSING:
            return False
        if topic == "execution":
            exec_id = str(item.get("execId") or "")
            return bool(
                exec_id
                and any(fill.execution_id == exec_id for fill in record.close_fills)
            )
        if topic == "order":
            return str(item.get("orderStatus") or "") == "Filled"
        if topic == "position":
            return bool(
                record.close_fills
                and _decimal(item.get("size"), default="0") == 0
            )
        return False

    def _mark_terminalization_retry(
        self, record: DemoExecutionRecord, exc: DemoExchangeError,
    ) -> None:
        warning = f"close terminalization retry required: {_sanitized_error(exc)}"
        self.last_error = warning
        self.last_terminalization_warning = warning
        if record.last_error == warning:
            return
        self.terminalization_retry_warnings += 1
        record.last_error = warning
        record.last_reconciliation_at = datetime.now(timezone.utc)
        record.updated_at = record.last_reconciliation_at
        self.repository.save_demo_execution(
            record, event_type="CLOSE_TERMINALIZATION_RETRY_REQUIRED"
        )

    def _record_terminalization_invariant(
        self,
        record: DemoExecutionRecord,
        *,
        realtime: list[dict[str, Any]],
        history: list[dict[str, Any]],
        executions: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> None:
        current = now or datetime.now(timezone.utc)
        evidence_at = record.closed_at or record.updated_at
        age = max(0.0, (current - _aware(evidence_at)).total_seconds())
        if age < self.settings.v2_terminalization_warning_seconds:
            return
        close_order = next((
            item for item in history
            if record.close_order_id
            and str(item.get("orderId") or "") == record.close_order_id
        ), None)
        attributed_qty = sum((
            _decimal(item.get("execQty"), default="0")
            for item in executions
            if record.close_order_id
            and str(item.get("orderId") or "") == record.close_order_id
            and _exchange_event_time_ms(item) >= _record_entry_time_ms(record)
        ), Decimal("0"))
        blockers: list[str] = []
        if any(
            str(item.get("symbol") or "") == record.symbol.value
            and _decimal(item.get("size"), default="0") > 0
            for item in positions
        ):
            blockers.append("remote position is not flat")
        if any(_is_execution_owned_open_order(item, record) for item in realtime):
            blockers.append("owned close/protection order remains open")
        if close_order is None:
            blockers.append("exact close order is unavailable")
        elif str(close_order.get("orderStatus") or "") != "Filled":
            blockers.append("exact close order is not Filled")
        if attributed_qty != record.accepted_quantity:
            blockers.append(
                "attributed close quantity does not equal owned entry quantity"
            )
        if not blockers:
            blockers.append("atomic terminal persistence did not complete")
        record.terminalization_blockers = list(dict.fromkeys(blockers))
        event_type = None
        if record.terminalization_warning_at is None:
            record.terminalization_warning_at = current
            event_type = "CLOSE_TERMINALIZATION_WARNING"
            self.terminalization_retry_warnings += 1
        if (
            age >= self.settings.v2_terminalization_hard_failure_seconds
            and record.terminalization_hard_failure_at is None
        ):
            record.terminalization_hard_failure_at = current
            event_type = "CLOSE_TERMINALIZATION_HARD_FAILURE"
        warning = (
            f"close terminalization unresolved after {int(age)}s: "
            + "; ".join(record.terminalization_blockers)
        )
        record.last_error = warning
        record.last_reconciliation_at = current
        record.updated_at = current
        self.last_error = warning
        self.last_terminalization_warning = warning
        if record.terminalization_hard_failure_at is not None:
            self.terminalization_hard_failures[str(record.id)] = list(
                record.terminalization_blockers
            )
        if event_type:
            self.repository.save_demo_execution(record, event_type=event_type)

    def retry_stuck_terminalizations(
        self, *, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Retry aged CLOSING records with read-only REST reconciliation."""
        current = now or datetime.now(timezone.utc)
        retried: list[str] = []
        resolved: list[str] = []
        for record in self.repository.load_demo_executions():
            if (
                record.state != DemoExecutionState.DEMO_CLOSING
                or not record.close_fills
                or current - _aware(record.closed_at or record.updated_at)
                < timedelta(seconds=self.settings.v2_terminalization_warning_seconds)
            ):
                continue
            retried.append(str(record.id))
            reconciled = self._reconcile_execution_rest(record)
            if reconciled.state in TERMINAL_DEMO_STATES:
                resolved.append(str(record.id))
        return {
            "retried_execution_ids": retried,
            "resolved_execution_ids": resolved,
            "hard_failures": dict(self.terminalization_hard_failures),
            "exchange_mutations_performed": False,
        }

    def reconcile(self) -> dict[str, Any]:
        if not self.enabled or self.client is None:
            return {"status": "DISABLED"}
        monotonic_now = time.monotonic()
        gap_limit = max(
            30.0,
            float(self.settings.demo_reconciliation_interval_seconds) * 2.0,
        )
        if (
            self._last_reconcile_monotonic is not None
            and monotonic_now - self._last_reconcile_monotonic > gap_limit
        ):
            self.sleep_resume_reconciliations += 1
            self._discard_ws_before_ms = int(time.time() * 1000)
        self._last_reconcile_monotonic = monotonic_now
        remote_orders = self.client.get_open_orders(settle_coin="USDT")
        history = self.client.get_order_history(settle_coin="USDT")
        executions = self.client.get_executions(settle_coin="USDT")
        closed_pnl = self.client.get_closed_pnl(settle_coin="USDT")
        self.last_reconciliation_at = datetime.now(timezone.utc)
        local = self.repository.load_demo_executions()
        unexpected_errors: list[Exception] = []
        for record in list(local):
            if record.state in TERMINAL_DEMO_STATES:
                continue
            try:
                self._reconcile_execution_rest(record)
            except DemoExchangeError as exc:
                self._mark_terminalization_retry(record, exc)
            except Exception as exc:
                self.last_error = _sanitized_error(exc)
                unexpected_errors.append(exc)
        local = self.repository.load_demo_executions()
        positions = self.client.get_positions(settle_coin="USDT")
        local_links = {
            link: item
            for item in local
            for link in (item.order_link_id, item.close_order_link_id)
            if link
        }
        bot_prefix = f"{self.settings.demo_order_link_prefix}-"
        owned_protection_ids = {
            str(order.get("orderId") or "")
            for order in remote_orders
            if any(
                _is_owned_bybit_protection_order(order, record, positions)
                for record in local
                if record.state == DemoExecutionState.DEMO_POSITION_OPEN
            )
        }
        self.bot_owned_open_orders = sum(
            str(item.get("orderLinkId") or "").startswith(bot_prefix)
            or str(item.get("orderId") or "") in owned_protection_ids
            for item in remote_orders
        )
        self.unrelated_open_orders = len(remote_orders) - self.bot_owned_open_orders
        remote_by_link: dict[str, list[dict[str, Any]]] = {}
        for order in [*remote_orders, *history]:
            link = str(order.get("orderLinkId") or "")
            if link:
                remote_by_link.setdefault(link, []).append(order)
        if any(len(items) > 1 and len({str(x.get("orderId")) for x in items}) > 1
               for link, items in remote_by_link.items() if link.startswith(bot_prefix)):
            self.reconciliation_incidents += 1
            self._activate_kill_switch("duplicate bot-created Demo entry order")
        for order in [*remote_orders, *history]:
            link = str(order.get("orderLinkId") or "")
            if link.startswith(bot_prefix) and link not in local_links:
                self.reconciliation_incidents += 1
                self._activate_kill_switch("remote bot-created order is missing locally")
            record = local_links.get(link)
            if record is not None:
                self._apply_order_update(record, order)
        for execution in executions:
            link = str(execution.get("orderLinkId") or "")
            record = local_links.get(link)
            if record:
                self._apply_fill(record, execution)
            else:
                order_meta = next((
                    order for order in history
                    if str(order.get("orderId") or "")
                    == str(execution.get("orderId") or "")
                ), {})
                close_record = self._attributable_close_record(
                    {**order_meta, **execution}, local
                )
                if close_record:
                    self._apply_fill(close_record, execution, force_close=True)
        now = datetime.now(timezone.utc)
        for record in local:
            if (
                record.state in {
                    DemoExecutionState.DEMO_SUBMITTING,
                    DemoExecutionState.DEMO_ACCEPTED,
                    DemoExecutionState.DEMO_ORDER_ACKNOWLEDGED,
                }
                and record.order_link_id not in remote_by_link
                and now - _aware(record.updated_at) > timedelta(
                    seconds=self.settings.demo_order_confirmation_timeout_seconds
                )
            ):
                record.state = DemoExecutionState.DEMO_RECONCILIATION_REQUIRED
                record.last_error = "locally pending Demo order is missing remotely"
                record.updated_at = now
                self.repository.save_demo_execution(record, event_type="REMOTE_ORDER_MISSING")
                self.reconciliation_incidents += 1
                self._activate_kill_switch("local Demo order is missing remotely")
        active_positions = [p for p in positions if _decimal(p.get("size"), default="0") > 0]
        active_owned_symbols = {
            item.symbol.value for item in local
            if item.state not in {
                *TERMINAL_DEMO_STATES,
            }
        }
        self.bot_owned_open_positions = sum(
            str(item.get("symbol") or "") in active_owned_symbols
            for item in active_positions
        )
        for position in active_positions:
            symbol = str(position.get("symbol") or "")
            owned = next(
                (item for item in local if item.symbol.value == symbol and item.state not in {
                    *TERMINAL_DEMO_STATES,
                }), None,
            )
            if owned is None:
                self.reconciliation_incidents += 1
                self._activate_kill_switch("unattributed remote Demo position")
            elif owned.state == DemoExecutionState.DEMO_POSITION_OPEN:
                remote_size = _decimal(position.get("size"), default="0")
                if owned.accepted_quantity > 0 and remote_size != owned.accepted_quantity:
                    self.reconciliation_incidents += 1
                    self._activate_kill_switch("remote Demo position quantity mismatch")
                elif datetime.now(timezone.utc) - _aware(
                    owned.first_fill_at
                    or owned.exchange_fill_at
                    or owned.position_confirmed_at
                    or owned.created_at
                ) >= timedelta(
                    seconds=(
                        owned.maximum_holding_seconds
                        or self.settings.paper_position_timeout_minutes * 60
                    )
                ):
                    self._submit_reduce_only_close(owned, remote_size, "maximum_holding_time")
                elif not _protection_present(position):
                    self._emergency_close(owned, "position protection is missing")
        active_symbols = {str(item.get("symbol") or "") for item in active_positions}
        for record in local:
            if (
                record.symbol.value not in active_symbols
                and record.state in {
                    DemoExecutionState.DEMO_POSITION_OPEN,
                    DemoExecutionState.DEMO_CLOSING,
                }
            ):
                # Never select PnL or a close by symbol.  The local list was
                # loaded before account-wide REST processing and can lag a WS
                # close fill.  Re-run the canonical exact order/exec ownership
                # path against this reconciliation's authoritative snapshots.
                if self._finalize_attributed_flat_close(
                    record,
                    realtime=remote_orders,
                    history=history,
                    executions=executions,
                    positions=positions,
                ):
                    continue
                if self._finalize_durable_exact_flat_close(
                    record, realtime=remote_orders, positions=positions
                ):
                    continue
                record.state = DemoExecutionState.DEMO_RECONCILIATION_REQUIRED
                record.last_error = (
                    "remote position is flat without exact execution-scoped "
                    "close order and fill evidence"
                )
                record.updated_at = datetime.now(timezone.utc)
                self.repository.save_demo_execution(record, event_type="REST_POSITION_RECONCILED")
        result = {
            "status": "OK" if not self.kill_switch_active else "BLOCKED",
            "remote_orders": len(remote_orders),
            "bot_owned_open_orders": self.bot_owned_open_orders,
            "unrelated_open_orders": self.unrelated_open_orders,
            "remote_positions": len(active_positions),
            "incidents": self.reconciliation_incidents,
            "open_orders_by_symbol": dict(self.symbol_open_order_counts),
            "usdt_order_reconciliation": (
                "PASS" if self.usdt_order_reconciliation_ok else "UNAVAILABLE"
            ),
            "usdt_position_reconciliation": (
                "PASS" if self.usdt_position_reconciliation_ok else "UNAVAILABLE"
            ),
        }
        if unexpected_errors:
            raise RuntimeError(
                "unexpected Demo reconciliation programming failure"
            ) from unexpected_errors[0]
        return result

    def cleanup_bot_owned(self) -> dict[str, int]:
        if not self.enabled or self.client is None:
            return {"orders_cancelled": 0, "positions_closed": 0}
        cancelled = 0
        closed = 0
        prefix = f"{self.order_prefix}-"
        local = self.repository.load_demo_executions()
        local_by_symbol = {
            item.symbol: item for item in local
            if item.run_id == self.run_id
            if item.state not in {DemoExecutionState.DEMO_CLOSED, DemoExecutionState.DEMO_FAILED}
        }
        for order in self.client.get_open_orders(settle_coin="USDT"):
            link = str(order.get("orderLinkId") or "")
            if not link.startswith(prefix):
                continue
            symbol = Symbol(str(order["symbol"]))
            self.client.cancel_order(symbol, str(order["orderId"]))
            cancelled += 1
        for position in self.client.get_positions(settle_coin="USDT"):
            size = _decimal(position.get("size"), default="0")
            if size <= 0:
                continue
            symbol = Symbol(str(position["symbol"]))
            record = local_by_symbol.get(symbol)
            if record is None:
                continue
            attributable_size = (
                min(size, record.accepted_quantity)
                if record.accepted_quantity > 0 else size
            )
            self._submit_reduce_only_close(record, attributable_size, "runner_cleanup")
            closed += 1
        return {"orders_cancelled": cancelled, "positions_closed": closed}

    def canary_execution_status(self, execution_id: str) -> dict[str, Any] | None:
        """Return a sanitized, targeted view of one current-run Demo canary."""
        if not self.enabled or self.client is None:
            return None
        record = next(
            (
                item for item in self.repository.load_demo_executions()
                if str(item.id) == execution_id and item.run_id == self.run_id
            ),
            None,
        )
        if record is None:
            return None
        positions = [
            item for item in self.client.get_positions(symbol=record.symbol)
            if _decimal(item.get("size"), default="0") > 0
        ]
        remote_position = positions[0] if len(positions) == 1 else None
        order_history = self.client.get_order_history(symbol=record.symbol)
        executions = self.client.get_executions(symbol=record.symbol)
        entry_orders = [
            _order_audit(item) for item in order_history
            if _matches_exchange_identity(
                item, record.order_id, record.order_link_id
            )
        ]
        close_orders = [
            _order_audit(item) for item in order_history
            if _matches_exchange_identity(
                item, record.close_order_id, record.close_order_link_id
            )
        ]
        entry_executions = [
            _execution_audit(item) for item in executions
            if _matches_exchange_identity(
                item, record.order_id, record.order_link_id
            )
        ]
        close_executions = [
            _execution_audit(item) for item in executions
            if _matches_exchange_identity(
                item, record.close_order_id, record.close_order_link_id
            )
        ]
        load_events = getattr(self.repository, "load_demo_execution_events", None)
        return {
            "execution": record.model_dump(mode="json"),
            "remote_position": (
                {
                    "symbol": str(remote_position.get("symbol") or ""),
                    "size": str(remote_position.get("size") or "0"),
                    "leverage": str(remote_position.get("leverage") or ""),
                    "take_profit": str(remote_position.get("takeProfit") or ""),
                    "stop_loss": str(remote_position.get("stopLoss") or ""),
                    "position_idx": int(remote_position.get("positionIdx") or 0),
                }
                if remote_position else None
            ),
            "entry_order_history": entry_orders,
            "entry_executions": entry_executions,
            "close_order_history": close_orders,
            "close_executions": close_executions,
            "remote_position_observations": [
                {
                    "symbol": str(item.get("symbol") or ""),
                    "size": str(item.get("size") or "0"),
                    "average_price": str(item.get("avgPrice") or ""),
                    "take_profit": str(item.get("takeProfit") or ""),
                    "stop_loss": str(item.get("stopLoss") or ""),
                }
                for item in positions
            ],
            "durable_state_transitions": (
                load_events(str(record.id)) if callable(load_events) else []
            ),
            "functional_result": (
                "FAIL"
                if record.failure_reason
                else (
                    "PASS"
                    if record.state == DemoExecutionState.DEMO_CLOSED
                    else "IN_PROGRESS"
                )
            ),
            "safety_cleanup_result": (
                "PASS"
                if record.state == DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE
                and record.cleanup_result
                else (
                    "NOT_REQUIRED"
                    if not record.failure_reason
                    else "IN_PROGRESS"
                )
            ),
        }

    def canary_cached_status(self, execution_id: str) -> dict[str, Any] | None:
        """Fast durable-only polling view; never performs exchange I/O."""
        record = next((
            item for item in self.repository.load_demo_executions()
            if str(item.id) == execution_id and item.run_id == self.run_id
        ), None)
        if record is None:
            return None
        return {
            "execution": record.model_dump(mode="json"),
            "last_reconciliation_at": (
                (self.last_reconciliation_at or record.last_reconciliation_at).isoformat()
                if (self.last_reconciliation_at or record.last_reconciliation_at) else None
            ),
            "reconciliation_in_progress": self.reconciliation_in_progress,
            "remote_position": None,
            "durable_cached_state": True,
        }

    def request_canary_failure_cleanup(
        self, execution_id: str, reason: str
    ) -> DemoExecutionRecord | None:
        """Persist functional failure and make a best-effort idempotent flat close."""
        if not self.enabled or self.client is None or not self.settings.demo_canary_enabled:
            return None
        require_demo_execution(self.settings)
        record = next(
            (
                item for item in self.repository.load_demo_executions()
                if str(item.id) == execution_id and item.run_id == self.run_id
            ),
            None,
        )
        if record is None:
            return None
        record.failure_reason = reason[:250]
        record.last_error = record.failure_reason
        record.updated_at = datetime.now(timezone.utc)
        self.repository.save_demo_execution(
            record, event_type="CANARY_FUNCTIONAL_FAILURE"
        )
        positions = [
            item for item in self.client.get_positions(symbol=record.symbol)
            if _decimal(item.get("size"), default="0") > 0
        ]
        if len(positions) == 1:
            self._submit_reduce_only_close(
                record,
                _decimal(positions[0].get("size")),
                "failure_cleanup",
            )
        self.reconcile()
        refreshed = self.repository.find_demo_execution(
            record.close_order_link_id or record.order_link_id,
            record.close_order_id or record.order_id or "",
        )
        return refreshed or record

    def direct_cleanup_execution(
        self, execution_id: str, reason: str
    ) -> DemoExecutionRecord:
        """One-shot exact-execution cleanup for an unavailable FastAPI process."""
        if (
            not self.enabled
            or self.client is None
            or not (self.settings.demo_canary_enabled or self.settings.v2_enabled)
        ):
            raise DemoSafetyError("direct Demo cleanup is unavailable")
        require_demo_execution(self.settings)
        validate_demo_domains(self.client.base_url, self.client.private_ws_url)
        record = next((
            item for item in self.repository.load_demo_executions()
            if str(item.id) == execution_id
        ), None)
        if record is None or record.state in TERMINAL_DEMO_STATES:
            raise DemoSafetyError("exact unresolved Demo execution was not found")
        positions = self.client.get_positions(symbol=record.symbol)
        active = [
            item for item in positions if _decimal(item.get("size"), default="0") > 0
        ]
        if len(active) > 1:
            raise DemoSafetyError("multiple remote positions prevent direct cleanup")
        orders = self.client.get_open_orders(symbol=record.symbol)
        associated = [
            order for order in orders
            if _is_owned_bybit_protection_order(order, record, positions)
        ]
        if any(order not in associated for order in orders):
            raise DemoSafetyError("unrelated remote order prevents direct cleanup")
        for order in associated:
            order_id = str(order.get("orderId") or "")
            if order_id:
                try:
                    self.client.cancel_order(record.symbol, order_id)
                except Exception:
                    # Bybit Full TP/SL is a coupled protection set: cancelling
                    # one generated order can remove its sibling as well.  A
                    # subsequent cancel then returns an error even though the
                    # requested safety state has already been reached.  Trust
                    # only a bounded authoritative re-read, never the error
                    # text, before treating that response as idempotent.
                    remaining = self.client.get_open_orders(symbol=record.symbol)
                    if any(
                        str(item.get("orderId") or "") == order_id
                        for item in remaining
                    ):
                        raise
        record.failure_reason = reason[:250]
        # Cancellation and a concurrently triggered protection order can
        # change the position.  Re-read immediately before the exact reduce-
        # only close and never use the pre-cancellation quantity blindly.
        active = [
            item for item in self.client.get_positions(symbol=record.symbol)
            if _decimal(item.get("size"), default="0") > 0
        ]
        if len(active) > 1:
            raise DemoSafetyError("multiple remote positions prevent direct cleanup")
        if active:
            remote_size = _decimal(active[0].get("size"), default="0")
            if record.accepted_quantity <= 0 or remote_size != record.accepted_quantity:
                raise DemoSafetyError("owned quantity mismatch prevents direct cleanup")
            self._submit_reduce_only_close(record, remote_size, "direct_restart_cleanup")
        self.reconcile()
        refreshed = next((
            item for item in self.repository.load_demo_executions()
            if str(item.id) == execution_id
        ), None)
        if refreshed is None:
            raise DemoSafetyError("direct cleanup result was not persisted")
        return refreshed

    def request_canary_close(self, execution_id: str) -> DemoExecutionRecord | None:
        """Submit one idempotent reduce-only close for a current-run canary."""
        if not self.enabled or self.client is None or not self.settings.demo_canary_enabled:
            return None
        require_demo_execution(self.settings)
        record = next(
            (
                item for item in self.repository.load_demo_executions()
                if str(item.id) == execution_id and item.run_id == self.run_id
            ),
            None,
        )
        if record is None:
            return None
        if record.state == DemoExecutionState.DEMO_CLOSED:
            return record
        positions = [
            item for item in self.client.get_positions(symbol=record.symbol)
            if _decimal(item.get("size"), default="0") > 0
        ]
        if len(positions) != 1:
            raise DemoSafetyError("canary close requires exactly one attributable position")
        remote_size = _decimal(positions[0].get("size"), default="0")
        if record.accepted_quantity <= 0 or remote_size != record.accepted_quantity:
            raise DemoSafetyError("canary local and remote quantities do not match")
        self._submit_reduce_only_close(record, remote_size, "canary_close")
        return record

    def request_canary_trailing_update(
        self,
        execution_id: str,
        *,
        close_before_mutation: bool = False,
    ) -> DemoExecutionRecord | None:
        """Exercise the production protection verifier on one owned canary."""
        if not self.enabled or self.client is None or not self.settings.demo_canary_enabled:
            return None
        require_demo_execution(self.settings)
        record = next(
            (
                item for item in self.repository.load_demo_executions()
                if str(item.id) == execution_id and item.run_id == self.run_id
            ),
            None,
        )
        if record is None:
            return None
        with self._execution_lock(record.id):
            record = next(
                (
                    item for item in self.repository.load_demo_executions()
                    if str(item.id) == execution_id and item.run_id == self.run_id
                ),
                record,
            )
            if record.state in TERMINAL_DEMO_STATES:
                return record
            if record.state != DemoExecutionState.DEMO_POSITION_OPEN:
                raise DemoSafetyError("trailing canary requires an open owned position")
            rules = self.client.get_instrument(record.symbol)
            position = _owned_position(
                self.client.get_positions(symbol=record.symbol), record
            )
            if position is None:
                raise DemoSafetyError("trailing canary position ownership is unavailable")
            current_tp = _decimal(position.get("takeProfit"), default="0")
            current_sl = _decimal(position.get("stopLoss"), default="0")
            mark_price = _decimal(position.get("markPrice"), default="0")
            if current_tp <= 0 or current_sl <= 0 or mark_price <= 0:
                raise DemoSafetyError("trailing canary protection or mark price is unavailable")
            if record.side == Side.BUY:
                ceiling = normalize_price(
                    mark_price - rules.tick_size * Decimal("2"),
                    rules,
                    round_up=False,
                )
                requested_sl = min(current_sl + rules.tick_size, ceiling)
                if requested_sl <= current_sl:
                    raise DemoSafetyError("no safe BUY trailing increment is available")
            else:
                floor = normalize_price(
                    mark_price + rules.tick_size * Decimal("2"),
                    rules,
                    round_up=True,
                )
                requested_sl = max(current_sl - rules.tick_size, floor)
                if requested_sl >= current_sl:
                    raise DemoSafetyError("no safe SELL trailing increment is available")

            before_mutation_hook = None
            if close_before_mutation:
                def close_exact_owned_position(
                    current: DemoExecutionRecord,
                ) -> None:
                    self._submit_reduce_only_close(
                        current,
                        current.accepted_quantity,
                        "canary_close",
                    )
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        reconciled = self._reconcile_execution_rest_locked(current)
                        positions = self.client.get_positions(
                            symbol=current.symbol
                        )
                        if (
                            reconciled.state in TERMINAL_DEMO_STATES
                            and not any(
                                _decimal(item.get("size"), default="0") > 0
                                for item in positions
                            )
                        ):
                            return
                        time.sleep(0.25)
                    raise DemoSafetyError(
                        "canary exact close did not become terminal before "
                        "the protection mutation"
                    )

                before_mutation_hook = close_exact_owned_position
            outcome = self._update_and_verify_protection(
                record,
                rules=rules,
                take_profit=current_tp,
                stop_loss=requested_sl,
                verified_at=datetime.now(timezone.utc),
                before_mutation_hook=before_mutation_hook,
            )
            return outcome.record

    def request_canary_flat_during_protection_race(
        self, execution_id: str
    ) -> DemoExecutionRecord | None:
        """Force one exact owned close between protection pre-check and mutation."""

        return self.request_canary_trailing_update(
            execution_id, close_before_mutation=True
        )

    def monitor_strategy_position(
        self,
        execution_id: str,
        last_price: Decimal,
        *,
        data_fresh: bool = True,
        setup_valid: bool = True,
        stale_feature: str | None = None,
        stale_age_seconds: float | None = None,
        stale_exit_threshold_seconds: float | None = None,
        now: datetime | None = None,
    ) -> DemoExecutionRecord | None:
        """Manage one exact owned V2 position; unrelated positions are untouched."""
        record = next(
            (item for item in self.repository.load_demo_executions() if str(item.id) == execution_id),
            None,
        )
        if record is None:
            return record
        with self._execution_lock(record.id):
            record = next(
                (
                    item for item in self.repository.load_demo_executions()
                    if str(item.id) == execution_id
                ),
                record,
            )
            return self._monitor_strategy_position_locked(
                record,
                last_price,
                data_fresh=data_fresh,
                setup_valid=setup_valid,
                stale_feature=stale_feature,
                stale_age_seconds=stale_age_seconds,
                stale_exit_threshold_seconds=stale_exit_threshold_seconds,
                now=now,
            )

    def _monitor_strategy_position_locked(
        self,
        record: DemoExecutionRecord,
        last_price: Decimal,
        *,
        data_fresh: bool,
        setup_valid: bool,
        stale_feature: str | None,
        stale_age_seconds: float | None,
        stale_exit_threshold_seconds: float | None,
        now: datetime | None,
    ) -> DemoExecutionRecord:
        current = now or datetime.now(timezone.utc)
        if record.state != DemoExecutionState.DEMO_POSITION_OPEN:
            return record
        if record.average_fill_price is None or record.accepted_quantity <= 0:
            return record
        direction = Decimal("1") if record.side == Side.BUY else Decimal("-1")
        move = direction * (last_price / record.average_fill_price - Decimal("1"))
        record.maximum_favorable_excursion = max(record.maximum_favorable_excursion, move)
        record.maximum_adverse_excursion = min(record.maximum_adverse_excursion, move)
        close_reason: str | None = None
        opened_at = (
            record.first_fill_at
            or record.exchange_fill_at
            or record.position_confirmed_at
            or record.created_at
        )
        if record.maximum_holding_seconds and (
            current - _aware(opened_at)
        ).total_seconds() >= record.maximum_holding_seconds:
            close_reason = "maximum_holding_time"
        elif not data_fresh:
            threshold = stale_exit_threshold_seconds or float(
                self.settings.v2_position_data_stale_exit_seconds
            )
            first_observation = record.position_data_stale_since is None
            if first_observation:
                record.position_data_stale_since = current
            record.position_data_stale_feature = stale_feature or "mandatory_market_data"
            record.position_data_stale_age_seconds = stale_age_seconds
            record.position_data_stale_threshold_seconds = threshold
            record.position_data_stale_protection_confirmed = record.protection_confirmed
            stale_duration = (
                current - _aware(record.position_data_stale_since)
            ).total_seconds()
            if first_observation:
                record.updated_at = current
                self.repository.save_demo_execution(
                    record, event_type="POSITION_DATA_STALE_OBSERVED"
                )
            if (
                not record.protection_confirmed
                or stale_duration >= threshold
                or (stale_age_seconds is not None and stale_age_seconds >= threshold)
            ):
                close_reason = "stale_signal"
        elif record.position_data_stale_since is not None:
            record.position_data_stale_since = None
            record.updated_at = current
            self.repository.save_demo_execution(
                record, event_type="POSITION_DATA_FRESH_RESTORED"
            )
        elif not setup_valid:
            close_reason = "invalidated_setup"
        if close_reason:
            self._submit_reduce_only_close(record, record.accepted_quantity, close_reason)
            return record
        if (
            self.client is not None
            and record.trailing_stop_pct is not None
            and record.take_profit is not None
            and record.stop_loss is not None
            and record.protection_confirmed
            and move > Decimal("0")
            and (
                record.trailing_stop_updated_at is None
                or (current - _aware(record.trailing_stop_updated_at)).total_seconds()
                >= self.settings.v2_trailing_update_interval_seconds
            )
        ):
            positions = [
                item for item in self.client.get_positions(record.symbol)
                if _decimal(item.get("size"), default="0") > 0
            ]
            owned = next((item for item in positions if (
                _decimal(item.get("size"), default="0") == record.accepted_quantity
                and str(item.get("side") or "").upper() == record.side.value
            )), None)
            if owned is not None:
                rules = self.client.get_instrument(record.symbol)
                trail = record.trailing_stop_pct / Decimal("100")
                proposed = (
                    normalize_price(last_price * (Decimal("1") - trail), rules, round_up=False)
                    if record.side == Side.BUY
                    else normalize_price(last_price * (Decimal("1") + trail), rules, round_up=True)
                )
                break_even_trigger = (record.break_even_at_r or Decimal("1")) * (
                    record.stop_loss_pct or Decimal("0")
                ) / Decimal("100")
                if move >= break_even_trigger:
                    costs = (
                        self.settings.v2_taker_fee_bps * Decimal("2")
                        + self.settings.v2_slippage_bps * Decimal("2")
                        + self.settings.v2_break_even_cost_buffer_bps
                    ) / Decimal("10000")
                    cost_adjusted_break_even = (
                        record.average_fill_price * (Decimal("1") + costs)
                        if record.side == Side.BUY
                        else record.average_fill_price * (Decimal("1") - costs)
                    )
                    proposed = (
                        max(proposed, cost_adjusted_break_even)
                        if record.side == Side.BUY
                        else min(proposed, cost_adjusted_break_even)
                    )
                improves = (
                    proposed > record.stop_loss if record.side == Side.BUY
                    else proposed < record.stop_loss
                )
                minimum_step = (
                    record.stop_loss
                    * self.settings.v2_trailing_update_min_bps
                    / Decimal("10000")
                )
                improves = improves and abs(proposed - record.stop_loss) >= minimum_step
                if improves:
                    # The break-even clamp above can introduce more precision
                    # than Bybit's tick. Canonicalize *after* every clamp before
                    # mutation and comparison.
                    proposed = normalize_price(
                        proposed,
                        rules,
                        round_up=record.side == Side.SELL,
                    )
                    outcome = self._update_and_verify_protection(
                        record,
                        rules=rules,
                        take_profit=record.take_profit,
                        stop_loss=proposed,
                        verified_at=current,
                    )
                    record = outcome.record
                    if outcome.terminalizing:
                        return record
        record.updated_at = current
        self.repository.save_demo_execution(record, event_type="V2_POSITION_METRICS_UPDATED")
        return record

    def _update_and_verify_protection(
        self,
        record: DemoExecutionRecord,
        *,
        rules: InstrumentRules,
        take_profit: Decimal,
        stop_loss: Decimal,
        verified_at: datetime,
        before_mutation_hook: Callable[[DemoExecutionRecord], None] | None = None,
    ) -> ProtectionVerificationOutcome:
        """Mutate once, then verify from bounded authoritative REST snapshots."""
        if self.client is None:
            raise DemoSafetyError("Demo exchange client is unavailable")
        round_up = record.side == Side.SELL
        normalized_tp = normalize_price(take_profit, rules, round_up=round_up)
        normalized_sl = normalize_price(stop_loss, rules, round_up=round_up)
        requested = {
            "take_profit": str(take_profit),
            "stop_loss": str(stop_loss),
            "active_price": None,
        }
        normalized = {
            "take_profit": str(normalized_tp),
            "stop_loss": str(normalized_sl),
            "tick_size": str(rules.tick_size),
        }

        # An identical update is idempotent and must not cause a second REST
        # mutation, including when two monitoring tasks race.
        before = self.client.get_positions(record.symbol)
        owned_before = _owned_position(before, record)
        if owned_before is not None and _normalized_protection_matches(
            owned_before, normalized_tp, normalized_sl, rules, record.side
        ):
            self._persist_protection_verification(
                record,
                requested=requested,
                normalized=normalized,
                position=owned_before,
                attempt=0,
                mutation_response=None,
                result="ALREADY_VERIFIED",
                blocker=None,
                observed_at=verified_at,
            )
            record.stop_loss = normalized_sl
            return ProtectionVerificationOutcome(record, True, False, 0)

        mutation_response: dict[str, Any] | None = None
        try:
            if before_mutation_hook is not None:
                before_mutation_hook(record)
            response = self.client.set_trading_stop(
                record.symbol, normalized_tp, normalized_sl
            )
            mutation_response = _sanitized_mutation_response(response)
        except DemoExchangeError as exc:
            if "not modified" in str(exc).lower():
                mutation_response = {
                    "retCode": exc.ret_code or "IDEMPOTENT",
                    "retMsg": exc.ret_msg or "not modified",
                }
            elif _is_structured_flat_position_error(exc):
                return self._handle_possible_flat_protection_failure(
                    record,
                    rules=rules,
                    requested=requested,
                    normalized=normalized,
                    take_profit=normalized_tp,
                    stop_loss=normalized_sl,
                    mutation_error=exc,
                    verified_at=verified_at,
                )
            else:
                raise

        attempts = self.settings.v2_protection_verification_attempts
        for attempt in range(1, attempts + 1):
            positions = self.client.get_positions(record.symbol)
            owned = _owned_position(positions, record)
            if owned is not None:
                matches = _normalized_protection_matches(
                    owned, normalized_tp, normalized_sl, rules, record.side
                )
                self._persist_protection_verification(
                    record,
                    requested=requested,
                    normalized=normalized,
                    position=owned,
                    attempt=attempt,
                    mutation_response=mutation_response,
                    result="VERIFIED" if matches else "REST_PROPAGATION_PENDING",
                    blocker=None if matches else "authoritative REST protection differs",
                    observed_at=datetime.now(timezone.utc),
                )
                if matches:
                    record.take_profit = normalized_tp
                    record.stop_loss = normalized_sl
                    record.trailing_stop_updated_at = verified_at
                    record.trailing_stop_update_count += 1
                    record.updated_at = verified_at
                    self.repository.save_demo_execution(
                        record, event_type="V2_TRAILING_STOP_UPDATED"
                    )
                    return ProtectionVerificationOutcome(record, True, False, attempt)
            else:
                self._persist_protection_verification(
                    record,
                    requested=requested,
                    normalized=normalized,
                    position=None,
                    attempt=attempt,
                    mutation_response=mutation_response,
                    result="POSITION_FLAT_RECONCILING",
                    blocker=None,
                    observed_at=datetime.now(timezone.utc),
                )
                reconciled = self._reconcile_execution_rest_locked(record)
                if reconciled.state in TERMINAL_DEMO_STATES or reconciled.state in {
                    DemoExecutionState.DEMO_CLOSING,
                    DemoExecutionState.DEMO_RECONCILIATION_REQUIRED,
                }:
                    return ProtectionVerificationOutcome(
                        reconciled, False, True, attempt
                    )
                # A zero REST position is never made safer by demanding TP/SL.
                # Terminalization owns exact close attribution from this point.
                if not any(
                    _decimal(item.get("size"), default="0") > 0
                    for item in positions
                ):
                    return ProtectionVerificationOutcome(
                        reconciled, False, True, attempt
                    )
            if attempt < attempts and self.settings.v2_protection_verification_delay_ms:
                time.sleep(
                    self.settings.v2_protection_verification_delay_ms / 1000
                )

        self._persist_protection_verification(
            record,
            requested=requested,
            normalized=normalized,
            position=owned,
            attempt=attempts,
            mutation_response=mutation_response,
            result="FAILED_OPEN_UNPROTECTED",
            blocker="open position protection did not match after bounded REST verification",
            observed_at=datetime.now(timezone.utc),
        )
        raise DemoSafetyError("updated trailing protection could not be verified")

    def _handle_possible_flat_protection_failure(
        self,
        record: DemoExecutionRecord,
        *,
        rules: InstrumentRules,
        requested: dict[str, Any],
        normalized: dict[str, Any],
        take_profit: Decimal,
        stop_loss: Decimal,
        mutation_error: DemoExchangeError,
        verified_at: datetime,
    ) -> ProtectionVerificationOutcome:
        """Classify a structured zero-position rejection from exact REST state.

        The caller already owns the per-execution lock.  The exchange may close
        the position between the pre-check and mutation response, so this path
        never retries the mutation.  It either verifies an open protected
        position, hands exact full-close evidence to the existing terminalizer,
        or fails closed with durable attribution blockers.
        """

        mutation_response = {
            "retCode": mutation_error.ret_code,
            "retMsg": mutation_error.ret_msg,
        }
        attempts = self.settings.v2_protection_verification_attempts
        last_blockers: list[str] = []
        for attempt in range(1, attempts + 1):
            positions = self.client.get_positions(symbol=record.symbol)
            symbol_positions = [
                item for item in positions
                if str(item.get("symbol") or "") == record.symbol.value
                and _decimal(item.get("size"), default="0") > 0
            ]
            owned = _owned_position(symbol_positions, record)
            if owned is not None:
                if _normalized_protection_matches(
                    owned, take_profit, stop_loss, rules, record.side
                ):
                    self._persist_protection_verification(
                        record,
                        requested=requested,
                        normalized=normalized,
                        position=owned,
                        attempt=attempt,
                        mutation_response=mutation_response,
                        result="VERIFIED",
                        blocker=None,
                        observed_at=datetime.now(timezone.utc),
                        classification="PROTECTION_VERIFIED_REMOTE_OPEN",
                        cycle_failure_emitted=False,
                        terminalization_result=None,
                    )
                    record.take_profit = take_profit
                    record.stop_loss = stop_loss
                    return ProtectionVerificationOutcome(
                        record, True, False, attempt
                    )
                last_blockers = [
                    "authoritative remote position remains open",
                    "authoritative REST protection differs",
                ]
            elif symbol_positions:
                last_blockers = [
                    "remote position does not match owned side or quantity"
                ]
            else:
                open_orders = self.client.get_open_orders(symbol=record.symbol)
                if open_orders:
                    last_blockers = [
                        "authoritative remote open orders remain"
                    ]
                    if (
                        attempt < attempts
                        and self.settings.v2_protection_verification_delay_ms
                    ):
                        time.sleep(
                            self.settings.v2_protection_verification_delay_ms
                            / 1000
                        )
                    continue
                reconciled = self._reconcile_execution_rest_locked(record)
                last_blockers = _terminalization_handoff_blockers(
                    reconciled,
                    positions=self.client.get_positions(symbol=record.symbol),
                    open_orders=open_orders,
                    all_records=self.repository.load_demo_executions(),
                )
                if not last_blockers:
                    self._persist_protection_verification(
                        reconciled,
                        requested=requested,
                        normalized=normalized,
                        position=None,
                        attempt=attempt,
                        mutation_response=mutation_response,
                        result="TERMINALIZATION_HANDOFF",
                        blocker=None,
                        observed_at=datetime.now(timezone.utc),
                        classification="TERMINALIZATION_HANDOFF",
                        cycle_failure_emitted=False,
                        terminalization_result=reconciled.state.value,
                    )
                    return ProtectionVerificationOutcome(
                        reconciled, False, True, attempt
                    )
            if (
                attempt < attempts
                and self.settings.v2_protection_verification_delay_ms
            ):
                time.sleep(
                    self.settings.v2_protection_verification_delay_ms / 1000
                )

        blocker = "; ".join(last_blockers) or (
            "zero-position protection rejection could not be reconciled"
        )
        if not any(
            _decimal(item.get("size"), default="0") > 0
            for item in self.client.get_positions(symbol=record.symbol)
        ):
            record.attribution_failure_reason = blocker
        self._persist_protection_verification(
            record,
            requested=requested,
            normalized=normalized,
            position=None,
            attempt=attempts,
            mutation_response=mutation_response,
            result="REAL_PROTECTION_FAILURE",
            blocker=blocker,
            observed_at=datetime.now(timezone.utc),
            classification="REAL_PROTECTION_FAILURE",
            cycle_failure_emitted=True,
            terminalization_result=None,
        )
        raise DemoSafetyError(
            "structured zero-position protection rejection failed exact "
            f"reconciliation: {blocker}"
        )

    def _persist_protection_verification(
        self,
        record: DemoExecutionRecord,
        *,
        requested: dict[str, Any],
        normalized: dict[str, Any],
        position: dict[str, Any] | None,
        attempt: int,
        mutation_response: dict[str, Any] | None,
        result: str,
        blocker: str | None,
        observed_at: datetime,
        classification: str | None = None,
        cycle_failure_emitted: bool | None = None,
        terminalization_result: str | None = None,
    ) -> None:
        close_quantity = sum(
            (fill.quantity for fill in record.close_fills), Decimal("0")
        )
        observation = {
            "requested": requested,
            "normalized_requested": normalized,
            "observed": {
                "take_profit": str(position.get("takeProfit") or "") if position else None,
                "stop_loss": str(position.get("stopLoss") or "") if position else None,
                "size": str(position.get("size") or "0") if position else "0",
                "side": str(position.get("side") or "") if position else None,
            },
            "source": "REST",
            "position_state": "OPEN" if position is not None else "FLAT_OR_CLOSING",
            "verification_attempt": attempt,
            "mutation_response": mutation_response,
            "result": result,
            "classification": classification,
            "blocker": blocker,
            "cycle_failure_emitted": cycle_failure_emitted,
            "terminalization_result": terminalization_result,
            "execution_id": str(record.id),
            "close_order_id": record.close_order_id,
            "close_execution_ids": [
                fill.execution_id for fill in record.close_fills
            ],
            "close_quantity": str(close_quantity),
            "observed_at": observed_at.isoformat(),
        }
        record.last_protection_verification = observation
        record.protection_verification_history = [
            *record.protection_verification_history[-19:], observation
        ]
        record.updated_at = observed_at
        self.repository.save_demo_execution(
            record, event_type="V2_PROTECTION_VERIFICATION_ATTEMPT"
        )

    def as_status(self) -> dict[str, Any]:
        records = self.repository.load_demo_executions() if self.repository else []
        counts: dict[str, int] = {}
        for record in records:
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
        active_records = [
            record for record in records if record.state not in TERMINAL_DEMO_STATES
        ]
        active_remote_watermarks = [
            value
            for record in active_records
            if (
                value := (
                    record.position_confirmed_at
                    or record.first_fill_at
                    or record.exchange_fill_at
                    or record.exchange_order_created_at
                    or record.created_at
                )
            ) is not None
        ]
        latest_active_remote_watermark = (
            max(_aware(value) for value in active_remote_watermarks)
            if active_remote_watermarks else None
        )
        remote_state_authoritative = bool(
            not self.reconciliation_in_progress
            and self.last_reconciliation_at is not None
            and (
                latest_active_remote_watermark is None
                or _aware(self.last_reconciliation_at)
                >= latest_active_remote_watermark
            )
        )
        remote_state_snapshot_state = (
            "REFRESHING"
            if self.reconciliation_in_progress
            else "READY" if remote_state_authoritative else "STALE"
        )
        return {
            "enabled": self.enabled,
            "environment": "demo" if self.enabled else "disabled",
            "rest_domain": self.client.base_url if self.client else None,
            "private_ws_domain": self.client.private_ws_url if self.client else None,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_reasons": list(self.kill_switch_reasons),
            "kill_switch_activations": len(self.kill_switch_reasons),
            "websocket_connected": self.websocket_connected,
            "websocket_reconnects": self.websocket_reconnects,
            "reconciliation_incidents": self.reconciliation_incidents,
            "symbol_open_order_counts": dict(self.symbol_open_order_counts),
            "open_orders_by_symbol": dict(self.symbol_open_order_counts),
            "usdt_order_reconciliation_ok": self.usdt_order_reconciliation_ok,
            "usdt_position_reconciliation_ok": self.usdt_position_reconciliation_ok,
            "usdt_order_reconciliation": (
                "PASS" if self.usdt_order_reconciliation_ok else "UNAVAILABLE"
            ),
            "usdt_position_reconciliation": (
                "PASS" if self.usdt_position_reconciliation_ok else "UNAVAILABLE"
            ),
            "orders_submitted": self.orders_submitted,
            "orders_accepted": self.orders_accepted,
            "orders_rejected": self.orders_rejected,
            "partial_fills": self.partial_fills,
            "complete_fills": self.complete_fills,
            "states": counts,
            "bot_owned_open_orders": self.bot_owned_open_orders,
            "unrelated_open_orders": self.unrelated_open_orders,
            "bot_owned_open_positions": self.bot_owned_open_positions,
            "remote_state_authoritative": remote_state_authoritative,
            "remote_state_snapshot_state": remote_state_snapshot_state,
            "reconciliation_in_progress": self.reconciliation_in_progress,
            "active_execution_remote_watermark": (
                latest_active_remote_watermark.isoformat()
                if latest_active_remote_watermark else None
            ),
            "last_error": self.last_error,
            "account_verified": self.account_verified,
            "risk_capital_usdt": str(self.settings.demo_risk_capital_usdt),
            "leverage": self.settings.demo_leverage,
            "symbol_leverage": dict(self.symbol_leverage),
            "leverage_normalized": self.leverage_normalized,
            "account_margin_mode": self.account_margin_mode,
            "run_id": self.run_id,
            "order_link_prefix": self.order_prefix,
            "last_reconciliation_at": (
                self.last_reconciliation_at.isoformat()
                if self.last_reconciliation_at else None
            ),
            "sleep_resume_reconciliations": self.sleep_resume_reconciliations,
            "terminalization_retry_warnings": self.terminalization_retry_warnings,
            "last_terminalization_warning": self.last_terminalization_warning,
            "terminalization_hard_failures": dict(
                self.terminalization_hard_failures
            ),
            "rest_reconciliation_watermark_ms": self._discard_ws_before_ms,
        }

    def _apply_order_update(
        self, record: DemoExecutionRecord, item: dict[str, Any], *, force_close: bool = False
    ) -> None:
        status = str(item.get("orderStatus") or "")
        order_id = str(item.get("orderId") or "")
        order_link = str(item.get("orderLinkId") or "")
        is_close = force_close or bool(
            (record.close_order_id and order_id == record.close_order_id)
            or (record.close_order_link_id and order_link == record.close_order_link_id)
        )
        if is_close:
            if _exchange_event_time_ms(item) < _record_entry_time_ms(record):
                return
            if (
                status == "Filled"
                and record.state in TERMINAL_DEMO_STATES
                and (
                    (record.close_order_id and order_id == record.close_order_id)
                    or (
                        record.close_order_link_id
                        and order_link == record.close_order_link_id
                    )
                )
            ):
                return
            if order_id and not record.close_order_id:
                record.close_order_id = order_id
            attribute_exchange_close(record, item, source="order_update")
            if (
                status == "Filled"
                and record.state == DemoExecutionState.DEMO_CLOSING
                and record.close_order_id == order_id
            ):
                return
            if status == "Filled" and record.state not in TERMINAL_DEMO_STATES:
                record.state = DemoExecutionState.DEMO_CLOSING
            elif status in {"Rejected", "Cancelled", "Deactivated"}:
                record.state = DemoExecutionState.DEMO_RECONCILIATION_REQUIRED
                record.last_error = f"close order ended with status {status}"
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(record, event_type=f"CLOSE_{status or 'UNKNOWN'}")
            return
        # Entry history is commonly replayed after close history on reconnect.
        # Once close activity exists it is historical evidence only and must
        # never move the durable state back to FILLED/PROTECTION_PENDING.
        if (
            record.close_order_id
            or record.close_order_link_id
            or record.close_fills
            or record.state == DemoExecutionState.DEMO_CLOSING
            or record.state in TERMINAL_DEMO_STATES
        ):
            return
        if record.exchange_order_created_at is None and (
            item.get("createdTime") is not None or item.get("orderCreateTime") is not None
        ):
            record.exchange_order_created_at = _timestamp(
                item.get("createdTime") or item.get("orderCreateTime")
            )
        record.exchange_order_status = status or record.exchange_order_status
        if record.state in {
            DemoExecutionState.DEMO_FULLY_FILLED,
            DemoExecutionState.DEMO_PROTECTION_PENDING,
            *TERMINAL_DEMO_STATES,
        } or (
            record.protection_confirmed and record.state in {
            DemoExecutionState.DEMO_POSITION_OPEN,
            DemoExecutionState.DEMO_CLOSING,
            DemoExecutionState.DEMO_CLOSED,
            }
        ):
            return
        if status == "PartiallyFilled":
            record.state = DemoExecutionState.DEMO_PARTIALLY_FILLED
            self.partial_fills += 1
        elif status == "Filled":
            record.state = DemoExecutionState.DEMO_FULLY_FILLED
            record.accepted_quantity = _decimal(item.get("cumExecQty"), default=str(record.requested_quantity))
            avg = _decimal(item.get("avgPrice"), default="0")
            if avg > 0:
                record.average_fill_price = avg
            self.complete_fills += 1
        elif status in {"Rejected", "Cancelled", "Deactivated"}:
            if record.accepted_quantity > 0:
                record.state = DemoExecutionState.DEMO_FILLED
                record.last_error = f"entry remainder ended with status {status}"
            else:
                record.state = DemoExecutionState.DEMO_FAILED
                record.last_error = f"entry order ended with status {status}"
        record.updated_at = datetime.now(timezone.utc)
        event_type = (
            "DEMO_FULLY_FILLED"
            if record.state == DemoExecutionState.DEMO_FULLY_FILLED
            else f"ORDER_{status or 'UNKNOWN'}"
        )
        self.repository.save_demo_execution(record, event_type=event_type)
        # Protection is installed only by the REST reconciler after a fresh,
        # attributable non-zero position read.  Order events alone are stale-
        # prone and are insufficient authority for an exchange mutation.
        if record.state in {
            DemoExecutionState.DEMO_FILLED,
            DemoExecutionState.DEMO_FULLY_FILLED,
        }:
            self._install_protection(record)

    def _apply_position_update(
        self, record: DemoExecutionRecord, item: dict[str, Any]
    ) -> None:
        if str(item.get("symbol") or "") != record.symbol.value:
            return
        size = _decimal(item.get("size"), default="0")
        if size == 0 and record.state == DemoExecutionState.DEMO_CLOSING:
            # Flat is authoritative for exposure, but CLOSED waits for REST
            # closed-PnL reconciliation so fees/PnL are not invented.
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(record, event_type="POSITION_FLAT_PENDING_PNL")
        elif size > 0 and record.state == DemoExecutionState.DEMO_POSITION_OPEN:
            # Private position events may lag the REST response used to verify
            # TP/SL and can omit protection fields. Never close on one stale WS
            # event; the symbol-scoped REST reconciler is authoritative.
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(
                record, event_type="POSITION_WS_OBSERVED"
            )

    def _apply_fill(
        self,
        record: DemoExecutionRecord,
        item: dict[str, Any],
        *,
        force_close: bool = False,
    ) -> None:
        exec_id = str(item.get("execId") or "")
        qty = _decimal(item.get("execQty"), default="0")
        price = _decimal(item.get("execPrice"), default="0")
        if not exec_id or qty <= 0 or price <= 0:
            return
        if any(fill.execution_id == exec_id for fill in [*record.fills, *record.close_fills]):
            return
        local_received_at = datetime.now(timezone.utc)
        local_received_perf = perf_counter()
        fill = DemoFill(
            execution_id=exec_id,
            order_id=str(item.get("orderId") or ""),
            quantity=qty,
            price=price,
            fee=_decimal(item.get("execFee"), default="0"),
            fee_currency=item.get("feeCurrency"),
            executed_at=_timestamp(item.get("execTime")),
            local_received_at=local_received_at,
        )
        is_close_fill = force_close or bool(
            (record.close_order_id and fill.order_id == record.close_order_id)
            or (
                record.close_order_link_id
                and str(item.get("orderLinkId") or "") == record.close_order_link_id
            )
        )
        if is_close_fill:
            all_records = self.repository.load_demo_executions()
            if (
                int(fill.executed_at.timestamp() * 1000) < _record_entry_time_ms(record)
                or _exchange_identity_used_by_other(
                    all_records, record, fill.order_id, fill.execution_id
                )
            ):
                return
            if fill.order_id and not record.close_order_id:
                record.close_order_id = fill.order_id
            attribute_exchange_close(record, item, source="execution_fill")
            record.close_fills.append(fill)
            record.closed_at = max(
                (entry.executed_at for entry in record.close_fills), default=fill.executed_at
            )
            close_qty = sum((entry.quantity for entry in record.close_fills), Decimal("0"))
            close_value = sum(
                (entry.quantity * entry.price for entry in record.close_fills), Decimal("0")
            )
            if close_qty > 0 and record.average_fill_price is not None:
                close_average = close_value / close_qty
                record.average_close_price = close_average
                reference = record.take_profit or record.stop_loss or close_average
                record.exit_slippage = (
                    close_average - reference
                    if record.side == Side.BUY else reference - close_average
                )
                # Price proximity alone is never authoritative. Exact Bybit
                # order/execution metadata or a durable bot close reason owns
                # the externally visible attribution.
        else:
            if (
                record.close_order_id
                or record.close_order_link_id
                or record.close_fills
                or record.state == DemoExecutionState.DEMO_CLOSING
                or record.state in TERMINAL_DEMO_STATES
            ):
                return
            record.fills.append(fill)
            if record.first_fill_at is None:
                record.first_fill_at = fill.executed_at
                record.exchange_fill_at = fill.executed_at
                record.local_fill_received_at = local_received_at
                submit_perf = self._submit_started_monotonic.get(record.id)
                ack_perf = self._ack_received_monotonic.get(record.id)
                if submit_perf is not None:
                    record.order_submit_to_first_fill_ms = max(
                        0.0, (local_received_perf - submit_perf) * 1000
                    )
                elif record.local_submit_started_at is not None:
                    elapsed = (
                        local_received_at - record.local_submit_started_at
                    ).total_seconds() * 1000
                    if elapsed >= 0:
                        record.order_submit_to_first_fill_ms = elapsed
                if record.local_ack_received_at is None:
                    record.fill_before_ack = True
                    record.ack_to_first_fill_ms = None
                elif local_received_at < record.local_ack_received_at:
                    record.fill_before_ack = True
                    record.ack_to_first_fill_ms = None
                elif ack_perf is not None:
                    record.ack_to_first_fill_ms = (
                        local_received_perf - ack_perf
                    ) * 1000
                else:
                    record.ack_to_first_fill_ms = (
                        local_received_at - record.local_ack_received_at
                    ).total_seconds() * 1000
                self._submit_started_monotonic.pop(record.id, None)
                self._ack_received_monotonic.pop(record.id, None)
            total_qty = sum((entry.quantity for entry in record.fills), Decimal("0"))
            total_value = sum((entry.quantity * entry.price for entry in record.fills), Decimal("0"))
            record.accepted_quantity = total_qty
            record.average_fill_price = total_value / total_qty
            if total_qty >= record.requested_quantity and record.state not in {
                DemoExecutionState.DEMO_PROTECTION_PENDING,
                DemoExecutionState.DEMO_POSITION_OPEN,
                DemoExecutionState.DEMO_CLOSING,
                *TERMINAL_DEMO_STATES,
            }:
                record.state = DemoExecutionState.DEMO_FULLY_FILLED
                record.exchange_order_status = "Filled"
            if record.reference_entry_price is not None:
                record.entry_slippage = (
                    record.average_fill_price - record.reference_entry_price
                    if record.side == Side.BUY
                    else record.reference_entry_price - record.average_fill_price
                )
        record.exchange_fees = sum(
            (entry.fee for entry in [*record.fills, *record.close_fills]), Decimal("0")
        )
        if record.close_fills and record.average_fill_price is not None:
            close_qty = sum((entry.quantity for entry in record.close_fills), Decimal("0"))
            close_value = sum(
                (entry.quantity * entry.price for entry in record.close_fills), Decimal("0")
            )
            close_average = close_value / close_qty
            direction = Decimal("1") if record.side == Side.BUY else Decimal("-1")
            record.gross_realized_pnl = (
                (close_average - record.average_fill_price)
                * close_qty * direction
            )
            record.paper_shadow_pnl = (
                record.gross_realized_pnl - record.exchange_fees
            )
            record.realized_exchange_pnl = record.paper_shadow_pnl
        record.updated_at = datetime.now(timezone.utc)
        self.repository.save_demo_execution(
            record,
            event_type=(
                "DEMO_FULLY_FILLED"
                if not is_close_fill
                and record.state == DemoExecutionState.DEMO_FULLY_FILLED
                else "EXECUTION_FILL"
            ),
        )
        # See _apply_order_update: the next authoritative REST reconciliation
        # owns protection installation.
        if (
            not is_close_fill
            and record.state == DemoExecutionState.DEMO_FULLY_FILLED
            and not record.protection_confirmed
        ):
            self._install_protection(record)

    def _install_protection(self, record: DemoExecutionRecord) -> None:
        if self.client is None or record.average_fill_price is None:
            self._emergency_close(record, "average fill price unavailable")
            return
        if (
            record.close_order_id
            or record.close_order_link_id
            or record.close_fills
            or record.state == DemoExecutionState.DEMO_CLOSING
            or record.state in TERMINAL_DEMO_STATES
        ):
            return
        positions = self.client.get_positions(record.symbol)
        position = next(
            (
                item for item in positions
                if str(item.get("symbol") or "") == record.symbol.value
                and _decimal(item.get("size"), default="0") > 0
            ),
            None,
        )
        if position is None:
            record.last_reconciliation_at = datetime.now(timezone.utc)
            record.updated_at = record.last_reconciliation_at
            self.repository.save_demo_execution(
                record, event_type="PROTECTION_SKIPPED_POSITION_FLAT"
            )
            return
        remote_size = _decimal(position.get("size"), default="0")
        expected_side = "BUY" if record.side == Side.BUY else "SELL"
        remote_side = str(position.get("side") or "").upper()
        if remote_side and remote_side != expected_side:
            self._emergency_close(record, "remote position side mismatch")
            return
        if record.accepted_quantity <= 0 or remote_size != record.accepted_quantity:
            self._emergency_close(record, "remote position quantity mismatch")
            return
        rules = self.client.get_instrument(record.symbol)
        entry = record.average_fill_price
        tp_pct = (
            record.take_profit_pct
            or Decimal(str(self.settings.signal_default_take_profit_pct))
        ) / 100
        sl_pct = (
            record.stop_loss_pct
            or Decimal(str(self.settings.signal_default_stop_loss_pct))
        ) / 100
        if record.side == Side.BUY:
            take_profit = normalize_price(entry * (1 + tp_pct), rules, round_up=False)
            stop_loss = normalize_price(entry * (1 - sl_pct), rules, round_up=False)
        else:
            take_profit = normalize_price(entry * (1 - tp_pct), rules, round_up=True)
            stop_loss = normalize_price(entry * (1 + sl_pct), rules, round_up=True)
        record.state = DemoExecutionState.DEMO_PROTECTION_PENDING
        record.take_profit = take_profit
        record.stop_loss = stop_loss
        self.repository.save_demo_execution(record, event_type="PROTECTION_PENDING")
        try:
            if not _protection_matches(position, take_profit, stop_loss):
                try:
                    self.client.set_trading_stop(record.symbol, take_profit, stop_loss)
                except DemoExchangeError as exc:
                    if "not modified" not in str(exc).lower():
                        raise
            positions = self.client.get_positions(record.symbol)
            position = next(
                (item for item in positions if _decimal(item.get("size"), default="0") > 0),
                None,
            )
            if position is None or not _protection_matches(position, take_profit, stop_loss):
                raise DemoExchangeError("exchange-side TP/SL could not be verified")
            record.protection_confirmed = True
            record.tp_identifier = str(position.get("takeProfit") or take_profit)
            record.sl_identifier = str(position.get("stopLoss") or stop_loss)
            record.protection_position_idx = int(position.get("positionIdx") or 0)
            try:
                protection_orders = self.client.get_open_orders(symbol=record.symbol)
                _capture_protection_order_ownership(
                    record, protection_orders, positions
                )
            except Exception:
                # Position-level TP/SL is authoritative. Order IDs are captured
                # on the next bounded reconciliation if Bybit has not exposed
                # the generated conditional orders yet.
                pass
            record.state = DemoExecutionState.DEMO_POSITION_OPEN
            record.updated_at = datetime.now(timezone.utc)
            record.position_confirmed_at = record.updated_at
            record.protection_confirmed_at = record.updated_at
            self.repository.save_demo_execution(
                record, event_type="DEMO_POSITION_OPEN"
            )
        except Exception as exc:
            error = _sanitized_error(exc)
            if "can not set tp/sl/ts for zero position" in error.lower():
                record.last_error = "position became flat before TP/SL confirmation"
                record.updated_at = datetime.now(timezone.utc)
                self.repository.save_demo_execution(
                    record, event_type="PROTECTION_SKIPPED_POSITION_FLAT"
                )
                return
            self._emergency_close(record, error)

    def _emergency_close(self, record: DemoExecutionRecord, reason: str) -> None:
        quantity = record.accepted_quantity or record.requested_quantity
        try:
            self._submit_reduce_only_close(record, quantity, "protection_failure")
        finally:
            self._activate_kill_switch(f"unprotected position: {reason}")

    def _submit_reduce_only_close(
        self, record: DemoExecutionRecord, quantity: Decimal, reason: str
    ) -> None:
        if self.client is None or record.close_order_id or record.close_order_link_id:
            return
        link = deterministic_order_link_id(
            self.order_prefix, record.candidate_id, "emergency"
        )
        record.state = DemoExecutionState.DEMO_CLOSING
        record.close_order_link_id = link
        record.exit_attribution = canonical_exit_attribution(reason)
        record.close_reason = record.exit_attribution
        record.exit_attribution_evidence = {
            "source": "bot_reduce_only_close",
            "requested_reason": reason,
            "close_order_link_id": link,
            "owned_quantity": str(quantity),
        }
        record.attribution_failure_reason = None
        record.updated_at = datetime.now(timezone.utc)
        self.repository.save_demo_execution(record, event_type="CLOSE_SUBMITTING")
        try:
            response = self.client.create_order({
                "category": "linear", "symbol": record.symbol.value,
                "side": "Sell" if record.side == Side.BUY else "Buy",
                "orderType": "Market", "qty": _format_decimal(quantity),
                "timeInForce": "IOC", "positionIdx": "0", "reduceOnly": "true",
                "orderLinkId": link,
            })
            record.close_order_id = str(
                (response.get("result") or {}).get("orderId") or ""
            ) or None
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(record, event_type="CLOSE_ACK")
        except Exception as exc:
            record.state = DemoExecutionState.DEMO_RECONCILIATION_REQUIRED
            record.last_error = _sanitized_error(exc)
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(record, event_type="CLOSE_UNCERTAIN")

    def _validate_remote_entry_state(
        self,
        symbol: Symbol,
        *,
        positions: list[dict[str, Any]] | None = None,
        open_orders: list[dict[str, Any]] | None = None,
        all_positions: list[dict[str, Any]] | None = None,
    ) -> None:
        if self.client is None:
            raise DemoSafetyError("Demo client unavailable")
        active_positions = [
            item for item in (
                positions if positions is not None else self.client.get_positions(symbol=symbol)
            )
            if _decimal(item.get("size"), default="0") > 0
        ]
        if active_positions:
            raise DemoSafetyError("conflicting remote Demo position exists")
        active_orders = (
            open_orders
            if open_orders is not None
            else self.client.get_open_orders(symbol=symbol)
        )
        if active_orders:
            raise DemoSafetyError("conflicting active Demo order exists")
        active_account_positions = [
            item for item in (
                all_positions
                if all_positions is not None
                else self.client.get_positions(settle_coin="USDT")
            )
            if _decimal(item.get("size"), default="0") > 0
        ]
        max_positions = (
            self.settings.max_concurrent_positions
            if self.settings.v2_enabled else self.settings.paper_max_total_open_positions
        )
        if len(active_account_positions) >= max_positions:
            raise DemoSafetyError("maximum total Demo positions reached")

    def _enforce_risk_controls(
        self,
        symbol: Symbol,
        *,
        closed_pnl: list[dict[str, Any]] | None = None,
    ) -> None:
        """Apply configured cooldown and exchange-realized loss controls."""
        if self.client is None:
            raise DemoSafetyError("Demo client unavailable")
        now = datetime.now(timezone.utc)
        records = self.repository.load_demo_executions()
        completed = [item for item in records if item.state == DemoExecutionState.DEMO_CLOSED]
        if records:
            latest_entry = max(item.created_at for item in records)
            if now - _aware(latest_entry) < timedelta(
                seconds=(
                    self.settings.v2_global_entry_cooldown_seconds
                    if self.settings.v2_enabled
                    else self.settings.paper_global_entry_cooldown_seconds
                )
            ):
                raise DemoSafetyError("global entry cooldown is active")
        symbol_window = demo_symbol_cooldown_window(
            completed,
            symbol,
            (
                self.settings.v2_symbol_cooldown_seconds
                if self.settings.v2_enabled
                else self.settings.paper_symbol_cooldown_seconds
            ),
        )
        if symbol_window is not None:
            _, cooldown_until = symbol_window
            if now < cooldown_until:
                raise DemoSafetyError("symbol cooldown is active")
        closed = (
            closed_pnl
            if closed_pnl is not None
            else self.client.get_closed_pnl(settle_coin="USDT")
        )
        daily = Decimal("0")
        weekly = Decimal("0")
        pnl_timeline: list[tuple[datetime, Decimal]] = []
        for item in closed:
            closed_at = _timestamp(item.get("updatedTime") or item.get("createdTime"))
            pnl = _decimal(item.get("closedPnl"), default="0")
            pnl_timeline.append((closed_at, pnl))
            age = now - closed_at
            if age <= timedelta(days=1):
                daily += pnl
            if age <= timedelta(days=7):
                weekly += pnl
        capital = (
            self.settings.risk_capital_usdt
            if self.settings.v2_enabled else self.settings.demo_risk_capital_usdt
        )
        equity = capital
        peak_equity = capital
        maximum_drawdown_pct = Decimal("0")
        for _, pnl in sorted(pnl_timeline, key=lambda row: row[0]):
            equity += pnl
            peak_equity = max(peak_equity, equity)
            if peak_equity > 0:
                maximum_drawdown_pct = max(
                    maximum_drawdown_pct,
                    (peak_equity - equity) / peak_equity * Decimal("100"),
                )
        reasons: list[str] = []
        daily_limit = (
            self.settings.v2_max_daily_loss_pct
            if self.settings.v2_enabled
            else Decimal(str(self.settings.paper_max_daily_net_loss_pct))
        )
        weekly_limit = (
            self.settings.v2_max_weekly_loss_pct
            if self.settings.v2_enabled
            else Decimal(str(self.settings.paper_max_weekly_net_loss_pct))
        )
        drawdown_limit = (
            self.settings.v2_max_drawdown_pct
            if self.settings.v2_enabled
            else Decimal(str(self.settings.paper_max_account_drawdown_pct))
        )
        if daily <= -(capital * daily_limit / 100):
            reasons.append("maximum daily Demo net loss reached")
        if weekly <= -(capital * weekly_limit / 100):
            reasons.append("maximum weekly Demo net loss reached")
        if maximum_drawdown_pct >= drawdown_limit:
            reasons.append("maximum Demo account drawdown reached")
        if reasons:
            for reason in reasons:
                self._activate_kill_switch(reason)
            raise DemoSafetyError("; ".join(reasons))

    def _verify_leverage_and_mode(
        self,
        symbol: Symbol,
        *,
        desired_leverage: Decimal = Decimal("1"),
        positions: list[dict[str, Any]] | None = None,
    ) -> None:
        if self.client is None:
            raise DemoSafetyError("Demo client unavailable")
        current_positions = (
            positions if positions is not None else self.client.get_positions(symbol=symbol)
        )
        for item in current_positions:
            leverage = _decimal(item.get("leverage"), default="1")
            position_idx = int(item.get("positionIdx") or 0)
            if leverage != desired_leverage:
                raise DemoSafetyError("remote leverage does not match configured leverage")
            if position_idx != 0:
                raise DemoSafetyError("hedge position mode is not supported")

    def _attributable_close_record(
        self,
        item: dict[str, Any],
        records: list[DemoExecutionRecord] | None = None,
    ) -> DemoExecutionRecord | None:
        symbol = str(item.get("symbol") or "")
        side = str(item.get("side") or "").upper()
        all_records = records or self.repository.load_demo_executions()
        order_id = str(item.get("orderId") or "")
        exec_id = str(item.get("execId") or "")
        item_time = _exchange_event_time_ms(item)
        quantity = _decimal(
            item.get("execQty") or item.get("cumExecQty") or item.get("qty"),
            default="0",
        )
        if (
            str(item.get("reduceOnly") or "").lower() != "true"
            and _decimal(item.get("closedSize"), default="0") <= 0
        ):
            return None
        candidates = [
            record for record in all_records
            if record.symbol.value == symbol
            and record.state in {
                DemoExecutionState.DEMO_POSITION_OPEN,
                DemoExecutionState.DEMO_CLOSING,
                DemoExecutionState.DEMO_RECONCILIATION_REQUIRED,
            }
            and (
                not side
                or (record.side == Side.BUY and side == "SELL")
                or (record.side == Side.SELL and side == "BUY")
            )
            and item_time >= _record_entry_time_ms(record)
            and quantity > 0
            and quantity <= record.accepted_quantity
            and not _exchange_identity_used_by_other(
                all_records, record, order_id, exec_id
            )
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _activate_kill_switch(self, reason: str) -> None:
        reason = " ".join(reason.split())[:250]
        self.kill_switch_active = True
        if reason not in self.kill_switch_reasons:
            self.kill_switch_reasons.append(reason)
        self.last_error = reason
        saver = getattr(self.repository, "save_demo_kill_switch", None)
        if callable(saver):
            saver(True, self.kill_switch_reasons)


def _url_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> dict[str, Any]:
    request = Request(url, data=body, headers=headers, method=method)
    with urlopen(request, timeout=timeout) as response:  # nosec B310 - exact domain guarded
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise DemoExchangeError("Bybit Demo returned a non-object response")
    return data


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return _step_round(value, step, ROUND_DOWN)


def _step_round(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    if step <= 0:
        raise ValueError("exchange step must be positive")
    return (value / step).to_integral_value(rounding=rounding) * step


def _decimal(value: object, *, default: str | None = None) -> Decimal:
    if value in (None, ""):
        if default is None:
            raise DemoExchangeError("required exchange decimal is missing")
        value = default
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise DemoExchangeError("invalid exchange decimal") from exc


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _matches_exchange_identity(
    item: dict[str, Any], order_id: str | None, order_link_id: str | None
) -> bool:
    return bool(
        (order_id and str(item.get("orderId") or "") == order_id)
        or (
            order_link_id
            and str(item.get("orderLinkId") or "") == order_link_id
        )
    )


def _order_audit(item: dict[str, Any]) -> dict[str, str]:
    return {
        "order_id": str(item.get("orderId") or ""),
        "order_link_id": str(item.get("orderLinkId") or ""),
        "symbol": str(item.get("symbol") or ""),
        "side": str(item.get("side") or ""),
        "status": str(item.get("orderStatus") or ""),
        "quantity": str(item.get("qty") or ""),
        "cumulative_executed_quantity": str(item.get("cumExecQty") or ""),
        "average_price": str(item.get("avgPrice") or ""),
        "updated_time": str(item.get("updatedTime") or ""),
    }


def _execution_audit(item: dict[str, Any]) -> dict[str, str]:
    return {
        "execution_id": str(item.get("execId") or ""),
        "order_id": str(item.get("orderId") or ""),
        "order_link_id": str(item.get("orderLinkId") or ""),
        "symbol": str(item.get("symbol") or ""),
        "side": str(item.get("side") or ""),
        "quantity": str(item.get("execQty") or ""),
        "price": str(item.get("execPrice") or ""),
        "fee": str(item.get("execFee") or ""),
        "fee_currency": str(item.get("feeCurrency") or ""),
        "executed_at": str(item.get("execTime") or ""),
    }


def _sanitized_margin_mode(value: object) -> str:
    text = str(value or "unknown")[:64]
    sanitized = "".join(
        character if character.isalnum() or character in {"_", "-"} else "_"
        for character in text
    )
    return sanitized or "unknown"


def _timestamp(value: object) -> datetime:
    if value in (None, ""):
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def demo_symbol_cooldown_window(
    records: list[DemoExecutionRecord],
    symbol: Symbol,
    duration_seconds: int,
) -> tuple[datetime, datetime] | None:
    """Return the immutable close-based cooldown window for one symbol."""
    symbol_records = [
        item
        for item in records
        if item.state == DemoExecutionState.DEMO_CLOSED and item.symbol == symbol
    ]
    if not symbol_records:
        return None
    started_at = max(
        _aware(item.closed_at or item.updated_at) for item in symbol_records
    )
    return started_at, started_at + timedelta(seconds=duration_seconds)


def _event_key(topic: str, item: dict[str, Any]) -> str:
    stable = str(
        item.get("execId") or item.get("orderId") or item.get("updatedTime")
        or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    )
    return f"{topic}:{stable}:{item.get('orderStatus', '')}"


def _event_timestamp_ms(
    event: dict[str, Any], item: dict[str, Any]
) -> int | None:
    values = (
        item.get("execTime"),
        item.get("updatedTime"),
        item.get("creationTime"),
        event.get("creationTime"),
    )
    parsed: list[int] = []
    for value in values:
        if value in (None, ""):
            continue
        try:
            parsed.append(int(str(value)))
        except (TypeError, ValueError):
            continue
    return max(parsed) if parsed else None


def _exchange_event_time_ms(item: dict[str, Any]) -> int:
    for field in ("execTime", "updatedTime", "createdTime", "creationTime"):
        value = item.get(field)
        if value not in (None, ""):
            try:
                return int(str(value))
            except (TypeError, ValueError):
                return 0
    return 0


def _record_entry_time_ms(record: DemoExecutionRecord) -> int:
    if record.fills:
        return min(int(_aware(fill.executed_at).timestamp() * 1000) for fill in record.fills)
    return int(_aware(record.created_at).timestamp() * 1000)


def _exchange_identity_used_by_other(
    records: list[DemoExecutionRecord],
    owner: DemoExecutionRecord,
    order_id: str,
    execution_id: str,
) -> bool:
    for record in records:
        if record.id == owner.id:
            continue
        if order_id and order_id in {record.order_id, record.close_order_id}:
            return True
        for fill in [*record.fills, *record.close_fills]:
            if execution_id and fill.execution_id == execution_id:
                return True
            if order_id and fill.order_id == order_id:
                return True
    return False


def _execution_material_fingerprint(record: DemoExecutionRecord) -> tuple[Any, ...]:
    return (
        record.state.value,
        record.order_id,
        record.close_order_id,
        record.close_order_link_id,
        str(record.accepted_quantity),
        str(record.average_fill_price),
        str(record.average_close_price),
        str(record.realized_exchange_pnl),
        record.protection_confirmed,
        record.tp_order_id,
        record.sl_order_id,
        record.protection_position_idx,
        tuple(fill.execution_id for fill in record.fills),
        tuple(fill.execution_id for fill in record.close_fills),
    )


def _protection_present(position: dict[str, Any]) -> bool:
    return _decimal(position.get("takeProfit"), default="0") > 0 and _decimal(
        position.get("stopLoss"), default="0"
    ) > 0


def _protection_matches(
    position: dict[str, Any], take_profit: Decimal, stop_loss: Decimal
) -> bool:
    return (
        _decimal(position.get("takeProfit"), default="0") == take_profit
        and _decimal(position.get("stopLoss"), default="0") == stop_loss
    )


def _owned_position(
    positions: list[dict[str, Any]], record: DemoExecutionRecord
) -> dict[str, Any] | None:
    return next(
        (
            item for item in positions
            if str(item.get("symbol") or record.symbol.value) == record.symbol.value
            and _decimal(item.get("size"), default="0") == record.accepted_quantity
            and str(item.get("side") or "").upper() == record.side.value
        ),
        None,
    )


def _normalized_protection_matches(
    position: dict[str, Any],
    take_profit: Decimal,
    stop_loss: Decimal,
    rules: InstrumentRules,
    side: Side,
) -> bool:
    round_up = side == Side.SELL
    observed_tp = normalize_price(
        _decimal(position.get("takeProfit"), default="0"),
        rules,
        round_up=round_up,
    )
    observed_sl = normalize_price(
        _decimal(position.get("stopLoss"), default="0"),
        rules,
        round_up=round_up,
    )
    return observed_tp == take_profit and observed_sl == stop_loss


def _sanitized_mutation_response(response: dict[str, Any] | None) -> dict[str, Any]:
    response = response or {}
    return {
        "retCode": response.get("retCode", 0),
        "retMsg": str(response.get("retMsg") or "")[:120] or None,
    }


def _is_structured_flat_position_error(exc: DemoExchangeError) -> bool:
    """Recognize only Bybit's structured zero-position protection rejection."""

    return bool(
        exc.ret_code is not None
        and exc.ret_code != 0
        and "can not set tp/sl/ts for zero position"
        in str(exc.ret_msg or "").casefold()
    )


def _terminalization_handoff_blockers(
    record: DemoExecutionRecord,
    *,
    positions: list[dict[str, Any]],
    open_orders: list[dict[str, Any]],
    all_records: list[DemoExecutionRecord],
) -> list[str]:
    """Prove that exact, full-close terminalization already completed."""

    blockers: list[str] = []
    if record.state not in TERMINAL_DEMO_STATES:
        blockers.append(f"durable execution is not terminal: {record.state.value}")
    if any(
        str(item.get("symbol") or "") == record.symbol.value
        and _decimal(item.get("size"), default="0") > 0
        for item in positions
    ):
        blockers.append("authoritative remote position is not flat")
    if open_orders:
        blockers.append("authoritative remote open orders remain")
    if not record.order_id or not record.fills:
        blockers.append("exact entry order/fill evidence is incomplete")
    if not record.close_order_id or not record.close_fills:
        blockers.append("exact close order/fill evidence is incomplete")

    entry_quantity = sum(
        (fill.quantity for fill in record.fills), Decimal("0")
    )
    close_quantity = sum(
        (fill.quantity for fill in record.close_fills), Decimal("0")
    )
    if (
        record.accepted_quantity <= 0
        or entry_quantity != record.accepted_quantity
        or close_quantity != record.accepted_quantity
    ):
        blockers.append("entry/full-close quantity evidence is not exact")
    if record.fills and record.close_fills:
        entry_time = min(fill.executed_at for fill in record.fills)
        close_time = min(fill.executed_at for fill in record.close_fills)
        if _aware(close_time) < _aware(entry_time):
            blockers.append("close evidence predates entry")
    if any(
        fill.order_id != record.order_id
        or _exchange_identity_used_by_other(
            all_records, record, fill.order_id, fill.execution_id
        )
        for fill in record.fills
    ):
        blockers.append("entry order/execution ownership is conflicting")
    if any(
        fill.order_id != record.close_order_id
        or _exchange_identity_used_by_other(
            all_records, record, fill.order_id, fill.execution_id
        )
        for fill in record.close_fills
    ):
        blockers.append("close order/execution ownership is conflicting")
    if canonical_exit_attribution(
        record.exit_attribution or record.close_reason
    ) == "unattributed_external_close":
        blockers.append("close attribution is not authoritative")
    if record.realized_exchange_pnl is None:
        blockers.append("authoritative exchange PnL is unavailable")
    return blockers


def _is_execution_owned_open_order(
    order: dict[str, Any], execution: DemoExecutionRecord,
) -> bool:
    """Match an open entry/close/protection order to one exact execution."""

    order_id = str(order.get("orderId") or "")
    order_link = str(order.get("orderLinkId") or "")
    if order_id and order_id in {
        execution.order_id,
        execution.close_order_id,
        execution.tp_order_id,
        execution.sl_order_id,
    }:
        return True
    if order_link and order_link in {
        execution.order_link_id, execution.close_order_link_id,
    }:
        return True
    if str(order.get("symbol") or "") != execution.symbol.value:
        return False
    if str(order.get("reduceOnly") or "").lower() != "true":
        return False
    if str(order.get("closeOnTrigger") or "").lower() != "true":
        return False
    stop_type = str(order.get("stopOrderType") or "")
    if stop_type not in {"TakeProfit", "StopLoss"}:
        return False
    expected_side = "SELL" if execution.side == Side.BUY else "BUY"
    if str(order.get("side") or "").upper() != expected_side:
        return False
    if _decimal(order.get("qty"), default="0") != execution.accepted_quantity:
        return False
    expected_trigger = (
        execution.take_profit if stop_type == "TakeProfit" else execution.stop_loss
    )
    return expected_trigger is not None and _decimal(
        order.get("triggerPrice"), default="0"
    ) == expected_trigger


def _is_owned_bybit_protection_order(
    order: dict[str, Any],
    execution: DemoExecutionRecord,
    positions: list[dict[str, Any]],
) -> bool:
    """Attribute only Bybit-generated position TP/SL with full agreement."""
    if str(order.get("symbol") or "") != execution.symbol.value:
        return False
    expected_side = "SELL" if execution.side == Side.BUY else "BUY"
    if str(order.get("side") or "").upper() != expected_side:
        return False
    if str(order.get("reduceOnly") or "").lower() != "true":
        return False
    if str(order.get("closeOnTrigger") or "").lower() != "true":
        return False
    stop_type = str(order.get("stopOrderType") or "")
    if stop_type not in {"TakeProfit", "StopLoss"}:
        return False
    expected_create_type = (
        "CreateByTakeProfit" if stop_type == "TakeProfit" else "CreateByStopLoss"
    )
    if str(order.get("createType") or "") != expected_create_type:
        return False
    if int(order.get("positionIdx") or 0) != execution.protection_position_idx:
        return False
    expected_trigger_direction = (
        1 if execution.side == Side.BUY and stop_type == "TakeProfit"
        else 2 if execution.side == Side.BUY
        else 2 if stop_type == "TakeProfit"
        else 1
    )
    if int(order.get("triggerDirection") or 0) != expected_trigger_direction:
        return False
    if execution.accepted_quantity <= 0 or _decimal(
        order.get("qty"), default="0"
    ) != execution.accepted_quantity:
        return False
    position = next((
        item for item in positions
        if str(item.get("symbol") or "") == execution.symbol.value
        and _decimal(item.get("size"), default="0") == execution.accepted_quantity
        and str(item.get("side") or "").upper()
        == ("BUY" if execution.side == Side.BUY else "SELL")
    ), None)
    if position is None or execution.take_profit is None or execution.stop_loss is None:
        return False
    if not _protection_matches(position, execution.take_profit, execution.stop_loss):
        return False
    expected_trigger = (
        execution.take_profit if stop_type == "TakeProfit" else execution.stop_loss
    )
    return _decimal(order.get("triggerPrice"), default="0") == expected_trigger


def _capture_protection_order_ownership(
    execution: DemoExecutionRecord,
    orders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> bool:
    """Persist exact exchange IDs only after full position-level attribution."""

    changed = False
    for order in orders:
        if not _is_owned_bybit_protection_order(order, execution, positions):
            continue
        order_id = str(order.get("orderId") or "")
        if not order_id:
            continue
        stop_type = str(order.get("stopOrderType") or "")
        field = "tp_order_id" if stop_type == "TakeProfit" else "sl_order_id"
        if getattr(execution, field) != order_id:
            setattr(execution, field, order_id)
            changed = True
    if changed:
        execution.protection_orders_verified_at = datetime.now(timezone.utc)
    return changed


def _sanitized_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"
