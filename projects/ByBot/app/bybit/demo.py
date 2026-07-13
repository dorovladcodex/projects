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


class DemoExchangeClient(Protocol):
    base_url: str
    private_ws_url: str

    def verify_credentials(self) -> bool: ...
    def get_instrument(self, symbol: Symbol) -> InstrumentRules: ...
    def get_positions(self, symbol: Symbol | None = None) -> list[dict[str, Any]]: ...
    def get_open_orders(self, symbol: Symbol | None = None) -> list[dict[str, Any]]: ...
    def get_order_history(self, symbol: Symbol | None = None) -> list[dict[str, Any]]: ...
    def get_executions(self, symbol: Symbol | None = None) -> list[dict[str, Any]]: ...
    def get_closed_pnl(self, symbol: Symbol | None = None) -> list[dict[str, Any]]: ...
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

    def get_positions(self, symbol: Symbol | None = None) -> list[dict[str, Any]]:
        return self._list("/v5/position/list", symbol)

    def get_open_orders(self, symbol: Symbol | None = None) -> list[dict[str, Any]]:
        return self._list("/v5/order/realtime", symbol, {"openOnly": "0"})

    def get_order_history(self, symbol: Symbol | None = None) -> list[dict[str, Any]]:
        return self._list("/v5/order/history", symbol)

    def get_executions(self, symbol: Symbol | None = None) -> list[dict[str, Any]]:
        return self._list("/v5/execution/list", symbol)

    def get_closed_pnl(self, symbol: Symbol | None = None) -> list[dict[str, Any]]:
        return self._list("/v5/position/closed-pnl", symbol)

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

    def _list(
        self, path: str, symbol: Symbol | None, extra: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        params = {"category": "linear", **(extra or {})}
        if symbol is not None:
            params["symbol"] = symbol.value
        data = self._request("GET", path, params)
        result = data.get("result") or {}
        items = result.get("list") if isinstance(result, dict) else None
        return [item for item in (items or []) if isinstance(item, dict)]

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
        self.bot_owned_open_positions = 0
        self.account_verified = False
        self.last_reconciliation_at: datetime | None = None
        if self.enabled:
            require_demo_execution(settings)
            if client is None:
                raise DemoSafetyError("Demo exchange client is unavailable")
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
        require_demo_execution(self.settings)
        validate_demo_domains(self.client.base_url, self.client.private_ws_url)
        self.account_verified = self.client.verify_credentials()
        if not self.account_verified:
            raise DemoSafetyError("Demo API credentials could not be verified")
        local = self.repository.load_demo_executions()
        active_local_symbols = {
            item.symbol.value for item in local
            if item.run_id == self.run_id
            if item.state not in {
                DemoExecutionState.DEMO_CLOSED, DemoExecutionState.DEMO_FAILED
            }
        }
        for symbol_value in self.settings.allowed_symbols:
            symbol = Symbol(symbol_value)
            self.client.get_instrument(symbol)
            for position in self.client.get_positions(symbol):
                if _decimal(position.get("leverage"), default="1") != Decimal("1"):
                    raise DemoSafetyError(f"{symbol.value} leverage is not exactly 1")
                if int(position.get("positionIdx") or 0) != 0:
                    raise DemoSafetyError("hedge position mode is not supported")
                if (
                    _decimal(position.get("size"), default="0") > 0
                    and symbol.value not in active_local_symbols
                ):
                    self._activate_kill_switch("unattributed remote Demo position")
                    raise DemoSafetyError("unrelated open Demo position conflicts with preflight")
        return self.account_verified

    def submit_candidate(
        self,
        candidate: NewsSignalCandidate,
        preview: SignalRiskPreview,
        classification: NewsClassification,
        snapshot: MarketSnapshot,
    ) -> DemoExecutionRecord | None:
        if not self.enabled or self.client is None:
            return None
        require_demo_execution(self.settings)
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

        rules = self.client.get_instrument(candidate.symbol)
        quantity = normalize_quantity(Decimal(str(preview.capped_size)), rules)
        entry_reference = Decimal(str(
            snapshot.ask_price
            if candidate.final_action == NewsSignalAction.BUY else snapshot.bid_price
        ))
        validate_order_notional(quantity, entry_reference, rules)
        self._validate_remote_entry_state(candidate.symbol)
        self.client.set_leverage(candidate.symbol, Decimal("1"))
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
            record.state = DemoExecutionState.DEMO_ACCEPTED
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(record, event_type="CREATE_ACK")
            self.orders_submitted += 1
            self.orders_accepted += 1
            return record
        except Exception as exc:
            record.state = DemoExecutionState.DEMO_RECONCILIATION_REQUIRED
            record.last_error = _sanitized_error(exc)
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(record, event_type="CREATE_UNCERTAIN")
            self.orders_rejected += 1
            self.last_error = record.last_error
            self._activate_kill_switch("entry submission outcome is uncertain")
            return record

    def handle_private_event(self, event: dict[str, Any]) -> None:
        topic = str(event.get("topic") or "")
        for item in event.get("data") or []:
            if not isinstance(item, dict):
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

    def reconcile(self) -> dict[str, Any]:
        if not self.enabled or self.client is None:
            return {"status": "DISABLED"}
        remote_orders = self.client.get_open_orders()
        history = self.client.get_order_history()
        executions = self.client.get_executions()
        closed_pnl = self.client.get_closed_pnl()
        self.last_reconciliation_at = datetime.now(timezone.utc)
        local = self.repository.load_demo_executions()
        local_links = {item.order_link_id: item for item in local}
        prefix = f"{self.order_prefix}-"
        bot_prefix = f"{self.settings.demo_order_link_prefix}-"
        self.bot_owned_open_orders = sum(
            str(item.get("orderLinkId") or "").startswith(prefix)
            for item in remote_orders
        )
        remote_by_link: dict[str, list[dict[str, Any]]] = {}
        for order in [*remote_orders, *history]:
            link = str(order.get("orderLinkId") or "")
            if link:
                remote_by_link.setdefault(link, []).append(order)
        if any(len(items) > 1 and len({str(x.get("orderId")) for x in items}) > 1
               for link, items in remote_by_link.items() if link.startswith(prefix)):
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
        positions = self.client.get_positions()
        active_positions = [p for p in positions if _decimal(p.get("size"), default="0") > 0]
        active_owned_symbols = {
            item.symbol.value for item in local
            if item.state not in {
                DemoExecutionState.DEMO_CLOSED, DemoExecutionState.DEMO_FAILED
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
                    DemoExecutionState.DEMO_CLOSED, DemoExecutionState.DEMO_FAILED
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
                    record.state = DemoExecutionState.DEMO_RECONCILIATION_REQUIRED
                    record.last_error = "locally open position is flat remotely without closed PnL"
                else:
                    record.realized_exchange_pnl = _decimal(
                        pnl_item.get("closedPnl"), default="0"
                    )
                    record.state = DemoExecutionState.DEMO_CLOSED
                    record.close_reason = record.close_reason or "exchange_close"
                record.updated_at = datetime.now(timezone.utc)
                self.repository.save_demo_execution(record, event_type="REST_POSITION_RECONCILED")
        return {
            "status": "OK" if not self.kill_switch_active else "BLOCKED",
            "remote_orders": len(remote_orders),
            "remote_positions": len(active_positions),
            "incidents": self.reconciliation_incidents,
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
        for order in self.client.get_open_orders():
            link = str(order.get("orderLinkId") or "")
            if not link.startswith(prefix):
                continue
            symbol = Symbol(str(order["symbol"]))
            self.client.cancel_order(symbol, str(order["orderId"]))
            cancelled += 1
        for position in self.client.get_positions():
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
            "orders_submitted": self.orders_submitted,
            "orders_accepted": self.orders_accepted,
            "orders_rejected": self.orders_rejected,
            "partial_fills": self.partial_fills,
            "complete_fills": self.complete_fills,
            "states": counts,
            "bot_owned_open_orders": self.bot_owned_open_orders,
            "bot_owned_open_positions": self.bot_owned_open_positions,
            "last_error": self.last_error,
            "account_verified": self.account_verified,
            "risk_capital_usdt": str(self.settings.demo_risk_capital_usdt),
            "leverage": self.settings.demo_leverage,
            "run_id": self.run_id,
            "order_link_prefix": self.order_prefix,
            "last_reconciliation_at": (
                self.last_reconciliation_at.isoformat()
                if self.last_reconciliation_at else None
            ),
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
            if status == "Filled":
                record.state = DemoExecutionState.DEMO_CLOSING
            elif status in {"Rejected", "Cancelled", "Deactivated"}:
                record.state = DemoExecutionState.DEMO_RECONCILIATION_REQUIRED
                record.last_error = f"close order ended with status {status}"
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(record, event_type=f"CLOSE_{status or 'UNKNOWN'}")
            return
        if record.protection_confirmed and record.state in {
            DemoExecutionState.DEMO_POSITION_OPEN,
            DemoExecutionState.DEMO_CLOSING,
            DemoExecutionState.DEMO_CLOSED,
        }:
            return
        if status == "PartiallyFilled":
            record.state = DemoExecutionState.DEMO_PARTIALLY_FILLED
            self.partial_fills += 1
        elif status == "Filled":
            record.state = DemoExecutionState.DEMO_FILLED
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
        self.repository.save_demo_execution(record, event_type=f"ORDER_{status or 'UNKNOWN'}")
        if record.state == DemoExecutionState.DEMO_FILLED:
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
            if not _protection_present(item):
                self._emergency_close(record, "position update has no TP/SL")

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
        state_before_fill = record.state
        if is_close_fill:
            record.close_fills.append(fill)
            close_qty = sum((entry.quantity for entry in record.close_fills), Decimal("0"))
            close_value = sum(
                (entry.quantity * entry.price for entry in record.close_fills), Decimal("0")
            )
            if close_qty > 0 and record.average_fill_price is not None:
                close_average = close_value / close_qty
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
            record.fills.append(fill)
            total_qty = sum((entry.quantity for entry in record.fills), Decimal("0"))
            total_value = sum((entry.quantity * entry.price for entry in record.fills), Decimal("0"))
            record.accepted_quantity = total_qty
            record.average_fill_price = total_value / total_qty
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
        record.updated_at = datetime.now(timezone.utc)
        self.repository.save_demo_execution(record, event_type="EXECUTION_FILL")
        if (
            not is_close_fill
            and state_before_fill in {
                DemoExecutionState.DEMO_FAILED,
                DemoExecutionState.DEMO_RECONCILIATION_REQUIRED,
            }
        ):
            record.state = DemoExecutionState.DEMO_FILLED
            self._install_protection(record)

    def _install_protection(self, record: DemoExecutionRecord) -> None:
        if self.client is None or record.average_fill_price is None:
            self._emergency_close(record, "average fill price unavailable")
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
            self.client.set_trading_stop(record.symbol, take_profit, stop_loss)
            positions = self.client.get_positions(record.symbol)
            position = next((item for item in positions if _decimal(item.get("size"), default="0") > 0), None)
            if position is None or not _protection_matches(position, take_profit, stop_loss):
                raise DemoExchangeError("exchange-side TP/SL could not be verified")
            record.protection_confirmed = True
            record.tp_identifier = str(position.get("takeProfit") or take_profit)
            record.sl_identifier = str(position.get("stopLoss") or stop_loss)
            record.state = DemoExecutionState.DEMO_POSITION_OPEN
            record.updated_at = datetime.now(timezone.utc)
            self.repository.save_demo_execution(record, event_type="PROTECTION_CONFIRMED")
        except Exception as exc:
            self._emergency_close(record, _sanitized_error(exc))

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
            item for item in self.client.get_positions(symbol)
            if _decimal(item.get("size"), default="0") > 0
        ]
        if positions:
            raise DemoSafetyError("conflicting remote Demo position exists")
        if self.client.get_open_orders(symbol):
            raise DemoSafetyError("conflicting active Demo order exists")
        all_positions = [
            item for item in self.client.get_positions()
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
        closed = self.client.get_closed_pnl()
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
        positions = self.client.get_positions(symbol)
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
