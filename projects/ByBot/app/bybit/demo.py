from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import hmac
import json
import time
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
    DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED,
}


class DemoSafetyError(RuntimeError):
    pass


class DemoExchangeError(RuntimeError):
    pass


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


def require_demo_execution(settings: Settings) -> None:
    validate_demo_domains(
        settings.bybit_private_demo_base_url,
        settings.bybit_private_demo_ws_url,
    )
    if settings.execution_mode != ExecutionMode.BYBIT_DEMO:
        raise DemoSafetyError("Execution mode is not BYBIT_DEMO")
    if settings.app_env.lower() != "demo" or settings.test_mode:
        raise DemoSafetyError("Demo execution requires APP_ENV=demo and TEST_MODE=false")
    if not settings.bybit_demo_trading_enabled:
        raise DemoSafetyError("Demo trading is not explicitly enabled")
    if settings.bybit_live_trading_enabled or settings.bybit_enable_trading:
        raise DemoSafetyError("Live or generic Bybit trading flags are forbidden")
    if settings.demo_leverage != 1:
        raise DemoSafetyError("Demo leverage must be exactly 1")


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
            raise DemoExchangeError(
                f"Bybit Demo request failed: {data.get('retMsg', 'unknown error')}"
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
        self.enabled = settings.execution_mode == ExecutionMode.BYBIT_DEMO
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
        self.symbol_leverage: dict[str, dict[str, str]] = {}
        self.symbol_open_order_counts: dict[str, int] = {}
        self.usdt_order_reconciliation_ok = False
        self.usdt_position_reconciliation_ok = False
        self.leverage_normalized = False
        self.account_margin_mode: str | None = None
        self.last_reconciliation_at: datetime | None = None
        self._last_reconcile_monotonic: float | None = None
        self._discard_ws_before_ms: int | None = None
        self.sleep_resume_reconciliations = 0
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
            if item.state not in {
                DemoExecutionState.DEMO_CLOSED, DemoExecutionState.DEMO_FAILED
            }
        }
        local_order_links = {
            link
            for item in local
            for link in (item.order_link_id, item.close_order_link_id)
            if link
        }
        for symbol_value in self.settings.allowed_symbols:
            symbol = Symbol(symbol_value)
            self.client.get_instrument(symbol)
            positions = self.client.get_positions(symbol)
            open_orders = self.client.get_open_orders(symbol=symbol)
            self.symbol_open_order_counts[symbol.value] = len(open_orders)
            conflicts = [
                order for order in open_orders
                if str(order.get("orderLinkId") or "") not in local_order_links
            ]
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
                symbol, positions=positions, open_orders=open_orders
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

    def _ensure_symbol_leverage(
        self,
        symbol: Symbol,
        *,
        positions: list[dict[str, Any]] | None = None,
        open_orders: list[dict[str, Any]] | None = None,
    ) -> None:
        """Confirm 1x on both sides, normalizing only a completely flat Demo symbol."""
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
        if buy_leverage == Decimal("1") and sell_leverage == Decimal("1"):
            return
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
            self.client.set_leverage(symbol, Decimal("1"))
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
        if confirmed_buy != Decimal("1") or confirmed_sell != Decimal("1"):
            mode = self.account_margin_mode or "unknown"
            raise DemoSafetyError(
                f"{symbol.value} leverage could not be confirmed as 1x "
                f"(buy={_format_decimal(confirmed_buy)}, "
                f"sell={_format_decimal(confirmed_sell)}, margin_mode={mode})"
            )

    def submit_candidate(
        self,
        candidate: NewsSignalCandidate,
        preview: SignalRiskPreview,
        classification: NewsClassification,
        snapshot: MarketSnapshot,
        *,
        canary_plan: CanaryMinimumOrderPlan | None = None,
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
        self._enforce_risk_controls(candidate.symbol)
        existing = self.repository.get_demo_execution(str(candidate.id))
        if existing is not None:
            return existing

        entry_reference = Decimal(str(
            snapshot.ask_price
            if candidate.final_action == NewsSignalAction.BUY else snapshot.bid_price
        ))
        rules = self.client.get_instrument(candidate.symbol)
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
        self._validate_remote_entry_state(candidate.symbol)
        self._ensure_symbol_leverage(candidate.symbol)
        self._verify_leverage_and_mode(candidate.symbol)
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
        )
        reserved = self.repository.reserve_demo_execution(record)
        if reserved is None:
            self.last_error = "durable Demo execution reservation failed"
            return None
        if reserved is not None and reserved.id != record.id:
            return reserved
        try:
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
            if topic == "execution":
                self._apply_fill(record, item, force_close=attributed_close)
            elif topic == "order":
                self._apply_order_update(record, item)
            elif topic == "position":
                self._apply_position_update(record, item)

    def _reconcile_execution_rest(
        self, record: DemoExecutionRecord
    ) -> DemoExecutionRecord:
        """Reconcile one execution from all authoritative symbol-scoped REST data."""
        if self.client is None:
            return record
        realtime = self.client.get_open_orders(symbol=record.symbol)
        history = self.client.get_order_history(symbol=record.symbol)
        executions = self.client.get_executions(symbol=record.symbol)
        positions = self.client.get_positions(symbol=record.symbol)

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
        record.last_reconciliation_at = datetime.now(timezone.utc)
        record.updated_at = record.last_reconciliation_at
        self.repository.save_demo_execution(record, event_type="REST_ORDER_RECONCILED")
        return record

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
        for record in list(local):
            if record.state in TERMINAL_DEMO_STATES:
                continue
            try:
                self._reconcile_execution_rest(record)
            except Exception as exc:
                self.last_error = _sanitized_error(exc)
        local = self.repository.load_demo_executions()
        local_links = {
            link: item
            for item in local
            for link in (item.order_link_id, item.close_order_link_id)
            if link
        }
        bot_prefix = f"{self.settings.demo_order_link_prefix}-"
        self.bot_owned_open_orders = sum(
            str(item.get("orderLinkId") or "").startswith(bot_prefix)
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
                close_record = self._attributable_close_record(execution, local)
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
        positions = self.client.get_positions(settle_coin="USDT")
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
                elif datetime.now(timezone.utc) - _aware(owned.created_at) >= timedelta(
                    minutes=self.settings.paper_position_timeout_minutes
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
                pnl_item = next(
                    (
                        item for item in closed_pnl
                        if str(item.get("symbol") or "") == record.symbol.value
                        and (
                            not record.close_order_id
                            or str(item.get("orderId") or "") == record.close_order_id
                        )
                    ),
                    None,
                )
                if pnl_item is None:
                    if record.close_fills and record.paper_shadow_pnl is not None:
                        record.realized_exchange_pnl = record.paper_shadow_pnl
                        record.state = (
                            DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE
                            if record.failure_reason
                            else DemoExecutionState.DEMO_CLOSED
                        )
                    else:
                        record.state = DemoExecutionState.DEMO_RECONCILIATION_REQUIRED
                        record.last_error = "locally open position is flat remotely without closed PnL"
                else:
                    record.realized_exchange_pnl = _decimal(
                        pnl_item.get("closedPnl"), default="0"
                    )
                    record.state = (
                        DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE
                        if record.failure_reason
                        else DemoExecutionState.DEMO_CLOSED
                    )
                    record.close_reason = record.close_reason or "exchange_close"
                if record.state in {
                    DemoExecutionState.DEMO_CLOSED,
                    DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE,
                }:
                    record.cleanup_result = (
                        "remote position flat and bot-owned orders zero"
                        if self.bot_owned_open_orders == 0
                        else "remote position flat; bot-owned orders remain"
                    )
                record.updated_at = datetime.now(timezone.utc)
                self.repository.save_demo_execution(record, event_type="REST_POSITION_RECONCILED")
        return {
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

    def as_status(self) -> dict[str, Any]:
        records = self.repository.load_demo_executions() if self.repository else []
        counts: dict[str, int] = {}
        for record in records:
            counts[record.state.value] = counts.get(record.state.value, 0) + 1
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
            "rest_reconciliation_watermark_ms": self._discard_ws_before_ms,
        }

    def _apply_order_update(self, record: DemoExecutionRecord, item: dict[str, Any]) -> None:
        status = str(item.get("orderStatus") or "")
        order_id = str(item.get("orderId") or "")
        order_link = str(item.get("orderLinkId") or "")
        is_close = bool(
            (record.close_order_id and order_id == record.close_order_id)
            or (record.close_order_link_id and order_link == record.close_order_link_id)
        )
        if is_close:
            if order_id and not record.close_order_id:
                record.close_order_id = order_id
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
        fill = DemoFill(
            execution_id=exec_id,
            order_id=str(item.get("orderId") or ""),
            quantity=qty,
            price=price,
            fee=_decimal(item.get("execFee"), default="0"),
            fee_currency=item.get("feeCurrency"),
            executed_at=_timestamp(item.get("execTime")),
        )
        is_close_fill = force_close or bool(
            (record.close_order_id and fill.order_id == record.close_order_id)
            or (
                record.close_order_link_id
                and str(item.get("orderLinkId") or "") == record.close_order_link_id
            )
        )
        if is_close_fill:
            if fill.order_id and not record.close_order_id:
                record.close_order_id = fill.order_id
            record.close_fills.append(fill)
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
                if record.take_profit is not None and record.stop_loss is not None:
                    if (
                        (record.side == Side.BUY and close_average >= record.take_profit)
                        or (record.side == Side.SELL and close_average <= record.take_profit)
                    ):
                        record.close_reason = "take_profit"
                    elif (
                        (record.side == Side.BUY and close_average <= record.stop_loss)
                        or (record.side == Side.SELL and close_average >= record.stop_loss)
                    ):
                        record.close_reason = "stop_loss"
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
            record.paper_shadow_pnl = (
                (close_average - record.average_fill_price)
                * close_qty * direction - record.exchange_fees
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
        tp_pct = Decimal(str(self.settings.signal_default_take_profit_pct)) / 100
        sl_pct = Decimal(str(self.settings.signal_default_stop_loss_pct)) / 100
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
            record.state = DemoExecutionState.DEMO_POSITION_OPEN
            record.updated_at = datetime.now(timezone.utc)
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
        record.close_reason = reason
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

    def _validate_remote_entry_state(self, symbol: Symbol) -> None:
        if self.client is None:
            raise DemoSafetyError("Demo client unavailable")
        positions = [
            item for item in self.client.get_positions(symbol=symbol)
            if _decimal(item.get("size"), default="0") > 0
        ]
        if positions:
            raise DemoSafetyError("conflicting remote Demo position exists")
        if self.client.get_open_orders(symbol=symbol):
            raise DemoSafetyError("conflicting active Demo order exists")
        all_positions = [
            item for item in self.client.get_positions(settle_coin="USDT")
            if _decimal(item.get("size"), default="0") > 0
        ]
        if len(all_positions) >= self.settings.paper_max_total_open_positions:
            raise DemoSafetyError("maximum total Demo positions reached")

    def _enforce_risk_controls(self, symbol: Symbol) -> None:
        """Apply configured cooldown and exchange-realized loss controls."""
        if self.client is None:
            raise DemoSafetyError("Demo client unavailable")
        now = datetime.now(timezone.utc)
        records = self.repository.load_demo_executions()
        completed = [item for item in records if item.state == DemoExecutionState.DEMO_CLOSED]
        if records:
            latest_entry = max(item.created_at for item in records)
            if now - _aware(latest_entry) < timedelta(
                seconds=self.settings.paper_global_entry_cooldown_seconds
            ):
                raise DemoSafetyError("global entry cooldown is active")
        symbol_records = [item for item in completed if item.symbol == symbol]
        if symbol_records:
            latest_symbol_close = max(item.updated_at for item in symbol_records)
            if now - _aware(latest_symbol_close) < timedelta(
                seconds=self.settings.paper_symbol_cooldown_seconds
            ):
                raise DemoSafetyError("symbol cooldown is active")
        closed = self.client.get_closed_pnl(settle_coin="USDT")
        daily = Decimal("0")
        weekly = Decimal("0")
        for item in closed:
            closed_at = _timestamp(item.get("updatedTime") or item.get("createdTime"))
            pnl = _decimal(item.get("closedPnl"), default="0")
            age = now - closed_at
            if age <= timedelta(days=1):
                daily += pnl
            if age <= timedelta(days=7):
                weekly += pnl
        capital = self.settings.demo_risk_capital_usdt
        reasons: list[str] = []
        if daily <= -(capital * Decimal(str(self.settings.paper_max_daily_net_loss_pct)) / 100):
            reasons.append("maximum daily Demo net loss reached")
        if weekly <= -(capital * Decimal(str(self.settings.paper_max_weekly_net_loss_pct)) / 100):
            reasons.append("maximum weekly Demo net loss reached")
        if weekly <= -(capital * Decimal(str(self.settings.paper_max_account_drawdown_pct)) / 100):
            reasons.append("maximum Demo account drawdown reached")
        if reasons:
            for reason in reasons:
                self._activate_kill_switch(reason)
            raise DemoSafetyError("; ".join(reasons))

    def _verify_leverage_and_mode(self, symbol: Symbol) -> None:
        if self.client is None:
            raise DemoSafetyError("Demo client unavailable")
        positions = self.client.get_positions(symbol=symbol)
        for item in positions:
            leverage = _decimal(item.get("leverage"), default="1")
            position_idx = int(item.get("positionIdx") or 0)
            if leverage != Decimal("1"):
                raise DemoSafetyError("remote leverage is not exactly 1")
            if position_idx != 0:
                raise DemoSafetyError("hedge position mode is not supported")

    def _attributable_close_record(
        self,
        item: dict[str, Any],
        records: list[DemoExecutionRecord] | None = None,
    ) -> DemoExecutionRecord | None:
        symbol = str(item.get("symbol") or "")
        side = str(item.get("side") or "").upper()
        candidates = [
            record for record in (records or self.repository.load_demo_executions())
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


def _sanitized_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:300]}"
