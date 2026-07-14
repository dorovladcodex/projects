from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.bybit.demo import (
    DEMO_PRIVATE_WS_URL,
    DEMO_REST_URL,
    BybitDemoRestClient,
    DemoExecutionService,
    DemoSafetyError,
    InstrumentRules,
    deterministic_order_link_id,
    normalize_price,
    normalize_quantity,
    validate_demo_domains,
)
from app.config import Settings
from app.models import (
    Asset,
    CandidateLifecycleState,
    ClassificationStatus,
    DemoExecutionRecord,
    DemoExecutionState,
    DemoFill,
    ExecutionEnvironment,
    MarketConfirmation,
    MarketSnapshot,
    NewsClassification,
    NewsSignalAction,
    NewsSignalCandidate,
    Sentiment,
    Side,
    SignalRiskPreview,
    SignalDryRunResult,
    SimpleTrend,
    Symbol,
)
from app.db.persistence import PersistenceRepository
from app.signals.service import SignalCandidateService, risk_capital_for_execution


def demo_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "demo",
        "test_mode": False,
        "bot_mode": "BYBIT_DEMO",
        "execution_mode": "BYBIT_DEMO",
        "bybit_env": "demo",
        "bybit_api_key": "fake-demo-key",
        "bybit_api_secret": "fake-demo-secret",
        "bybit_demo_trading_enabled": True,
        "bybit_live_trading_enabled": False,
        "bybit_enable_trading": False,
        "auto_paper_execution": False,
        "paper_global_entry_cooldown_seconds": 0,
        "paper_symbol_cooldown_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


def instrument() -> InstrumentRules:
    return InstrumentRules(
        symbol=Symbol.BTCUSDT,
        status="Trading",
        qty_step=Decimal("0.001"),
        min_order_qty=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        tick_size=Decimal("0.10"),
        min_leverage=Decimal("1"),
        max_leverage=Decimal("100"),
        leverage_step=Decimal("0.01"),
    )


class MemoryRepository:
    def __init__(self) -> None:
        self.records = {}
        self.events: set[str] = set()
        self.kill = None
        self.saved_events = []

    def load_demo_kill_switch(self):
        return self.kill

    def save_demo_kill_switch(self, active, reasons):
        self.kill = {"active": active, "reasons": list(reasons)}
        return True

    def get_demo_execution(self, candidate_id):
        return self.records.get(candidate_id)

    def reserve_demo_execution(self, record):
        existing = self.records.get(str(record.candidate_id))
        if existing:
            return existing
        self.records[str(record.candidate_id)] = record.model_copy(deep=True)
        return record

    def save_demo_execution(self, record, *, event_type):
        self.records[str(record.candidate_id)] = record.model_copy(deep=True)
        self.saved_events.append((event_type, record.state.value))
        return True

    def load_demo_executions(self):
        return [item.model_copy(deep=True) for item in self.records.values()]

    def find_demo_execution(self, order_link_id, order_id):
        for item in self.records.values():
            if order_link_id in {item.order_link_id, item.close_order_link_id}:
                return item.model_copy(deep=True)
            if order_id in {item.order_id, item.close_order_id} and order_id:
                return item.model_copy(deep=True)
        return None

    def record_demo_event(self, key, event_type, payload):
        if key in self.events:
            return False
        self.events.add(key)
        return True


class FailingReservationRepository(MemoryRepository):
    def reserve_demo_execution(self, record):
        return None


class FakeDemoClient:
    base_url = DEMO_REST_URL
    private_ws_url = DEMO_PRIVATE_WS_URL

    def __init__(self) -> None:
        self.orders = []
        self.cancelled = []
        self.positions = [
            {"symbol": symbol, "size": "0", "leverage": "1.00", "positionIdx": 0}
            for symbol in ("BTCUSDT", "ETHUSDT")
        ]
        self.open_orders = []
        self.history = []
        self.executions = []
        self.closed_pnl = []
        self.protection_ok = True
        self.leverage_calls = []
        self.position_scopes = []
        self.open_order_scopes = []
        self.history_scopes = []
        self.execution_scopes = []
        self.closed_pnl_scopes = []

    def verify_credentials(self):
        return True

    def get_account_info(self):
        return {"marginMode": "REGULAR_MARGIN"}

    def get_instrument(self, symbol):
        return instrument()

    def get_positions(self, symbol=None, settle_coin=None):
        assert symbol is None or settle_coin is None
        self.position_scopes.append((symbol, settle_coin))
        if symbol is None:
            return list(self.positions)
        return [item for item in self.positions if item.get("symbol") == symbol.value]

    def get_open_orders(self, symbol=None, settle_coin=None):
        assert symbol is None or settle_coin is None
        self.open_order_scopes.append((symbol, settle_coin))
        if symbol is None:
            return list(self.open_orders)
        return [item for item in self.open_orders if item.get("symbol") == symbol.value]

    def get_order_history(self, symbol=None, settle_coin=None):
        self.history_scopes.append((symbol, settle_coin))
        return list(self.history)

    def get_executions(self, symbol=None, settle_coin=None):
        self.execution_scopes.append((symbol, settle_coin))
        return list(self.executions)

    def get_closed_pnl(self, symbol=None, settle_coin=None):
        self.closed_pnl_scopes.append((symbol, settle_coin))
        return list(self.closed_pnl)

    def set_leverage(self, symbol, leverage):
        assert leverage == Decimal("1")
        self.leverage_calls.append((symbol, leverage))
        for item in self.positions:
            if item.get("symbol") == symbol.value:
                item["leverage"] = "1"
                item["buyLeverage"] = "1"
                item["sellLeverage"] = "1"
        return {"retCode": 0}

    def create_order(self, payload):
        self.orders.append(dict(payload))
        return {"retCode": 0, "result": {"orderId": f"order-{len(self.orders)}"}}

    def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        return {"retCode": 0}

    def set_trading_stop(self, symbol, take_profit, stop_loss):
        if not self.protection_ok:
            raise RuntimeError("protection failure")
        self.positions = [{
            "symbol": symbol.value, "size": "0.010", "leverage": "1",
            "positionIdx": 0, "takeProfit": str(take_profit),
            "stopLoss": str(stop_loss),
        }]
        return {"retCode": 0}


class ImmediateFillClient(FakeDemoClient):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source

    def create_order(self, payload):
        response = super().create_order(payload)
        order_id = response["result"]["orderId"]
        self.positions = [{
            "symbol": payload["symbol"], "size": payload["qty"],
            "avgPrice": "65000", "leverage": "1", "positionIdx": 0,
        }]
        if self.source == "history":
            self.history = [{
                "symbol": payload["symbol"], "orderId": order_id,
                "orderLinkId": payload["orderLinkId"], "orderStatus": "Filled",
                "cumExecQty": payload["qty"], "avgPrice": "65000",
            }]
        elif self.source == "execution":
            self.executions = [{
                "symbol": payload["symbol"], "orderId": order_id,
                "orderLinkId": payload["orderLinkId"], "execId": "fill-1",
                "execQty": payload["qty"], "execPrice": "65000",
                "execFee": "0.03575", "feeCurrency": "USDT",
                "execTime": "1784040000000",
            }]
        return response


class LeveragePreparationClient(FakeDemoClient):
    """Stateful, network-free client for startup leverage preparation tests."""

    def __init__(
        self,
        *,
        leverage: str | dict[Symbol, str] = "1",
        position_size: str = "0",
        confirm_change: bool = True,
    ) -> None:
        super().__init__()
        self.current_leverage = (
            dict(leverage)
            if isinstance(leverage, dict)
            else {Symbol.BTCUSDT: leverage, Symbol.ETHUSDT: leverage}
        )
        self.position_size = position_size
        self.confirm_change = confirm_change
        self.set_leverage_calls: list[tuple[Symbol, Decimal]] = []
        self.position_queries: list[Symbol | None] = []

    def get_positions(self, symbol=None, settle_coin=None):
        assert symbol is None or settle_coin is None
        self.position_queries.append(symbol)
        if symbol is None:
            symbols = [Symbol.BTCUSDT, Symbol.ETHUSDT]
        else:
            symbols = [symbol]
        return [
            {
                "symbol": item.value,
                "side": "",
                "size": self.position_size,
                "leverage": self.current_leverage[item],
                "positionIdx": 0,
                "tradeMode": 0,
            }
            for item in symbols
        ]

    def get_open_orders(self, symbol=None, settle_coin=None):
        assert symbol is None or settle_coin is None
        if symbol is None:
            return list(self.open_orders)
        return [
            item for item in self.open_orders
            if item.get("symbol") in {None, symbol.value}
        ]

    def set_leverage(self, symbol, leverage):
        self.set_leverage_calls.append((symbol, leverage))
        if self.confirm_change:
            self.current_leverage[symbol] = str(leverage)
        return {"retCode": 0}


def candidate_bundle():
    news_id = uuid4()
    candidate = NewsSignalCandidate(
        news_id=news_id,
        execution_environment=ExecutionEnvironment.BYBIT_DEMO,
        symbol=Symbol.BTCUSDT,
        state=CandidateLifecycleState.READY,
        proposed_action=NewsSignalAction.BUY,
        final_action=NewsSignalAction.BUY,
        sentiment=Sentiment.BULLISH,
        classification_confidence=.95, news_importance=.9,
        category="etf", urgency="high",
        market_confirmation=MarketConfirmation(
            available=True, fresh=True, direction_confirmed=True
        ),
        expected_edge_bps=25, proposed_stop_loss_pct=.5,
        proposed_take_profit_pct=1, ttl_seconds=300,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    classification = NewsClassification(
        news_id=news_id, asset=Asset.BTC, sentiment=Sentiment.BULLISH,
        confidence=.95, category="etf", urgency="high", reason="approval",
        model_name="mock", classification_status=ClassificationStatus.SUCCESS,
        trade_eligible=True,
    )
    preview = SignalRiskPreview(
        preview_performed=True, approved=True, capped_size=.010,
        position_notional=Decimal("650"), max_allowed_notional=Decimal("1000"),
        risk_decision_id=1,
    )
    snapshot = MarketSnapshot(
        symbol=Symbol.BTCUSDT, timestamp=datetime.now(timezone.utc),
        last_price=65000, bid_price=64999.9, ask_price=65000.1,
        price_change_1m_pct=.2, simple_trend=SimpleTrend.BULLISH,
        trend_score=.5, volatility_pct=.2, liquidity_ok=True,
    )
    return candidate, classification, preview, snapshot


def service(client=None, repo=None):
    return DemoExecutionService(
        demo_settings(), repo or MemoryRepository(), client or FakeDemoClient(),
        run_id="demo-test-run",
    )


def test_exact_demo_domains_only() -> None:
    validate_demo_domains(DEMO_REST_URL, DEMO_PRIVATE_WS_URL)
    for rest, ws in [
        ("https://api.bybit.com", DEMO_PRIVATE_WS_URL),
        ("https://api-testnet.bybit.com", "wss://stream-testnet.bybit.com"),
    ]:
        with pytest.raises(DemoSafetyError):
            validate_demo_domains(rest, ws)
    with pytest.raises(DemoSafetyError):
        BybitDemoRestClient(
            api_key="fake", api_secret="fake",
            base_url="https://api.bybit.com",
            private_ws_url="wss://stream.bybit.com",
        )


def test_demo_startup_leverage_already_one_is_idempotent() -> None:
    client = LeveragePreparationClient(leverage="1.00")
    demo = service(client)

    assert demo.verify_account_and_environment() is True

    assert client.set_leverage_calls == []
    assert client.orders == []
    assert demo.symbol_leverage == {
        "BTCUSDT": {"buy": "1", "sell": "1"},
        "ETHUSDT": {"buy": "1", "sell": "1"},
    }
    assert demo.leverage_normalized is True
    assert demo.verify_account_and_environment() is True
    assert client.set_leverage_calls == []


def test_demo_startup_uses_symbol_and_usdt_scopes() -> None:
    client = FakeDemoClient()
    demo = service(client)

    assert demo.verify_account_and_environment() is True

    assert (Symbol.BTCUSDT, None) in client.open_order_scopes
    assert (Symbol.ETHUSDT, None) in client.open_order_scopes
    assert (None, "USDT") in client.open_order_scopes
    assert (Symbol.BTCUSDT, None) in client.position_scopes
    assert (Symbol.ETHUSDT, None) in client.position_scopes
    assert (None, "USDT") in client.position_scopes
    assert demo.as_status()["open_orders_by_symbol"] == {
        "BTCUSDT": 0,
        "ETHUSDT": 0,
    }
    assert demo.as_status()["usdt_order_reconciliation"] == "PASS"
    assert demo.as_status()["usdt_position_reconciliation"] == "PASS"
    assert client.orders == []


def test_demo_startup_allows_known_persisted_bot_order() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    assert record is not None
    client.open_orders = [{
        "symbol": "BTCUSDT",
        "orderId": record.order_id,
        "orderLinkId": record.order_link_id,
    }]

    restarted = service(client, repo)
    assert restarted.verify_account_and_environment() is True
    assert restarted.as_status()["open_orders_by_symbol"]["BTCUSDT"] == 1


def _protected_restart_fixture():
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, _, _, _ = candidate_bundle()
    record = DemoExecutionRecord(
        candidate_id=candidate.id, risk_decision_id=1, run_id=demo.run_id,
        order_link_id="entry-link", order_id="entry-id",
        state=DemoExecutionState.DEMO_POSITION_OPEN,
        symbol=Symbol.BTCUSDT, side=Side.BUY,
        requested_quantity=Decimal("0.001"), accepted_quantity=Decimal("0.001"),
        average_fill_price=Decimal("65000"), take_profit=Decimal("65650"),
        stop_loss=Decimal("64675"), protection_confirmed=True,
    )
    repo.records[str(candidate.id)] = record
    client.positions = [
        {
            "symbol": "BTCUSDT", "size": "0.001", "side": "Buy",
            "takeProfit": "65650", "stopLoss": "64675", "leverage": "1",
            "positionIdx": 0,
        },
        {
            "symbol": "ETHUSDT", "size": "0", "side": "", "leverage": "1",
            "positionIdx": 0,
        },
    ]
    client.open_orders = [
        {
            "symbol": "BTCUSDT", "orderId": "tp", "orderLinkId": "",
            "side": "Sell", "qty": "0.001", "reduceOnly": True,
            "closeOnTrigger": True, "stopOrderType": "TakeProfit",
            "triggerPrice": "65650",
        },
        {
            "symbol": "BTCUSDT", "orderId": "sl", "orderLinkId": "",
            "side": "Sell", "qty": "0.001", "reduceOnly": True,
            "closeOnTrigger": True, "stopOrderType": "StopLoss",
            "triggerPrice": "64675",
        },
    ]
    return demo, repo, client, record


def test_restart_accepts_bybit_protection_with_empty_order_link_id() -> None:
    demo, _, client, record = _protected_restart_fixture()

    assert demo.verify_account_and_environment() is True
    assert demo.kill_switch_active is False
    assert record.state == DemoExecutionState.DEMO_POSITION_OPEN
    assert client.orders == []


def test_restart_with_protection_still_blocks_unrelated_manual_order() -> None:
    demo, _, client, _ = _protected_restart_fixture()
    client.open_orders.append({
        "symbol": "BTCUSDT", "orderId": "manual", "orderLinkId": "manual",
        "side": "Sell", "qty": "0.001", "reduceOnly": False,
        "closeOnTrigger": False, "stopOrderType": "", "triggerPrice": "",
    })

    with pytest.raises(DemoSafetyError, match="unrelated active Demo order"):
        demo.verify_account_and_environment()
    assert demo.kill_switch_active is True
    assert client.orders == []


def test_restart_reconciliation_keeps_protected_state_monotonic() -> None:
    demo, repo, client, record = _protected_restart_fixture()
    client.history = [{
        "symbol": "BTCUSDT", "orderId": record.order_id,
        "orderLinkId": record.order_link_id, "orderStatus": "Filled",
        "cumExecQty": "0.001", "avgPrice": "65000",
    }]

    assert demo.verify_account_and_environment() is True
    demo.reconcile()

    saved = repo.get_demo_execution(str(record.candidate_id))
    assert saved.state == DemoExecutionState.DEMO_POSITION_OPEN
    assert saved.protection_confirmed is True
    assert not any(
        state in {"DEMO_FULLY_FILLED", "DEMO_PROTECTION_PENDING"}
        for _, state in repo.saved_events[-3:]
    )


def test_demo_startup_rejects_unattributed_active_order() -> None:
    client = FakeDemoClient()
    client.open_orders = [{
        "symbol": "BTCUSDT",
        "orderId": "manual-order",
        "orderLinkId": "manual-link",
    }]

    with pytest.raises(DemoSafetyError, match="unrelated active Demo order"):
        service(client).verify_account_and_environment()

    assert client.orders == []


def test_demo_startup_normalizes_flat_symbols_to_one() -> None:
    client = LeveragePreparationClient(
        leverage={Symbol.BTCUSDT: "10", Symbol.ETHUSDT: "2.00"}
    )

    assert service(client).verify_account_and_environment() is True

    assert client.set_leverage_calls == [
        (Symbol.BTCUSDT, Decimal("1")),
        (Symbol.ETHUSDT, Decimal("1")),
    ]
    assert client.current_leverage == {
        Symbol.BTCUSDT: "1",
        Symbol.ETHUSDT: "1",
    }
    # Successful preflight re-queries both symbols after normalization and
    # never submits an entry/close order.
    assert client.position_queries.count(Symbol.BTCUSDT) >= 2
    assert client.position_queries.count(Symbol.ETHUSDT) >= 2
    assert client.orders == []


def test_demo_set_leverage_sends_both_buy_and_sell_values() -> None:
    requests: list[tuple[str, str, dict[str, str]]] = []

    def fake_http(method, url, headers, body, timeout):
        del headers, timeout
        requests.append((method, url, json.loads(body.decode("utf-8"))))
        return {"retCode": 0, "result": {}}

    client = BybitDemoRestClient(
        api_key="fake", api_secret="fake", http_request=fake_http
    )
    client.set_leverage(Symbol.BTCUSDT, Decimal("1.00"))

    method, url, payload = requests[0]
    assert method == "POST"
    assert url == f"{DEMO_REST_URL}/v5/position/set-leverage"
    assert payload["buyLeverage"] == "1"
    assert payload["sellLeverage"] == "1"
    assert payload["symbol"] == "BTCUSDT"


def test_linear_open_orders_rejects_missing_scope_without_http() -> None:
    calls = []

    def fake_http(method, url, headers, body, timeout):
        calls.append(url)
        return {"retCode": 0, "result": {"list": []}}

    client = BybitDemoRestClient(
        api_key="fake", api_secret="fake", http_request=fake_http
    )

    with pytest.raises(DemoSafetyError, match="symbol or settleCoin"):
        client.get_open_orders()

    assert calls == []


def test_linear_list_requests_apply_symbol_or_usdt_scope() -> None:
    queries: list[dict[str, list[str]]] = []

    def fake_http(method, url, headers, body, timeout):
        queries.append(parse_qs(urlparse(url).query))
        return {"retCode": 0, "result": {"list": []}}

    client = BybitDemoRestClient(
        api_key="fake", api_secret="fake", http_request=fake_http
    )
    client.get_open_orders("btcusdt")
    client.get_open_orders(settle_coin="usdt")
    client.get_positions(Symbol.ETHUSDT)
    client.get_positions(settle_coin="USDT")

    assert queries[0]["symbol"] == ["BTCUSDT"]
    assert "settleCoin" not in queries[0]
    assert queries[1]["settleCoin"] == ["USDT"]
    assert queries[2]["symbol"] == ["ETHUSDT"]
    assert queries[3]["settleCoin"] == ["USDT"]
    assert all(query["category"] == ["linear"] for query in queries)


def test_v5_list_pagination_preserves_scope_and_deduplicates() -> None:
    queries: list[dict[str, list[str]]] = []

    def fake_http(method, url, headers, body, timeout):
        query = parse_qs(urlparse(url).query)
        queries.append(query)
        if "cursor" not in query:
            return {
                "retCode": 0,
                "result": {
                    "list": [{"orderId": "one"}, {"orderId": "shared"}],
                    "nextPageCursor": "page-2",
                },
            }
        return {
            "retCode": 0,
            "result": {
                "list": [{"orderId": "shared"}, {"orderId": "two"}],
                "nextPageCursor": "",
            },
        }

    client = BybitDemoRestClient(
        api_key="fake", api_secret="fake", http_request=fake_http
    )
    rows = client.get_order_history(settle_coin="USDT")

    assert [row["orderId"] for row in rows] == ["one", "shared", "two"]
    assert len(queries) == 2
    assert queries[0]["settleCoin"] == ["USDT"]
    assert queries[1]["settleCoin"] == ["USDT"]
    assert queries[1]["cursor"] == ["page-2"]


def test_v5_list_pagination_stops_repeated_cursor() -> None:
    calls = 0

    def fake_http(method, url, headers, body, timeout):
        nonlocal calls
        calls += 1
        return {
            "retCode": 0,
            "result": {
                "list": [{"execId": str(calls)}],
                "nextPageCursor": "same-cursor",
            },
        }

    client = BybitDemoRestClient(
        api_key="fake", api_secret="fake", http_request=fake_http
    )

    assert len(client.get_executions(settle_coin="USDT")) == 2
    assert calls == 2


def test_demo_startup_does_not_change_leverage_with_open_position() -> None:
    client = LeveragePreparationClient(leverage="10", position_size="0.01")

    with pytest.raises(DemoSafetyError, match="position"):
        service(client).verify_account_and_environment()

    assert client.set_leverage_calls == []
    assert client.orders == []


def test_demo_startup_does_not_change_leverage_with_open_order() -> None:
    client = LeveragePreparationClient(leverage="10")
    client.open_orders = [{
        "symbol": "BTCUSDT", "orderId": "existing", "orderStatus": "New"
    }]

    with pytest.raises(DemoSafetyError, match="order"):
        service(client).verify_account_and_environment()

    assert client.set_leverage_calls == []
    assert client.orders == []


def test_demo_startup_fails_when_one_leverage_cannot_be_confirmed() -> None:
    client = LeveragePreparationClient(leverage="10", confirm_change=False)

    with pytest.raises(DemoSafetyError, match="leverage"):
        service(client).verify_account_and_environment()

    assert client.set_leverage_calls == [(Symbol.BTCUSDT, Decimal("1"))]
    assert client.orders == []


@pytest.mark.parametrize(
    ("base_url", "private_ws_url"),
    [
        ("https://api.bybit.com", "wss://stream.bybit.com"),
        ("https://api-testnet.bybit.com", "wss://stream-testnet.bybit.com"),
    ],
)
def test_demo_startup_rejects_non_demo_client_domains(
    base_url: str, private_ws_url: str
) -> None:
    client = LeveragePreparationClient(leverage="10")
    client.base_url = base_url
    client.private_ws_url = private_ws_url

    with pytest.raises(DemoSafetyError, match="domain"):
        service(client).verify_account_and_environment()

    assert client.set_leverage_calls == []
    assert client.orders == []


def test_test_mode_and_live_configuration_are_rejected() -> None:
    with pytest.raises(ValidationError):
        demo_settings(test_mode=True)
    with pytest.raises(ValidationError):
        demo_settings(bybit_live_trading_enabled=True)
    with pytest.raises(ValidationError, match="DEMO_CANARY_ENABLED"):
        Settings(demo_canary_enabled=True, execution_mode="PAPER")


def test_demo_preflight_requires_both_leverage_sides_confirmed() -> None:
    class OneSidedClient(FakeDemoClient):
        def set_leverage(self, symbol, leverage):
            self.leverage_calls.append((symbol, leverage))
            for item in self.positions:
                if item.get("symbol") == symbol.value:
                    item["buyLeverage"] = "1"
                    item["sellLeverage"] = "10"
            return {"retCode": 0}

    client = OneSidedClient()
    client.positions[0].update({
        "leverage": "10", "buyLeverage": "10", "sellLeverage": "10"
    })
    demo = service(client)
    with pytest.raises(DemoSafetyError, match="could not be confirmed as 1x"):
        demo.verify_account_and_environment()
    assert client.orders == []


def test_decimal_instrument_normalization() -> None:
    rules = instrument()
    assert normalize_quantity(Decimal("0.01099"), rules) == Decimal("0.010")
    assert normalize_price(Decimal("65000.19"), rules, round_up=False) == Decimal("65000.1")
    assert normalize_price(Decimal("65000.11"), rules, round_up=True) == Decimal("65000.2")


def test_demo_sizing_capital_does_not_use_wallet_or_paper_equity() -> None:
    settings = demo_settings(demo_risk_capital_usdt=Decimal("10000"))
    assert risk_capital_for_execution(settings, paper_equity=987654.32) == 10000


def test_create_acknowledgement_is_not_a_fill_and_duplicate_is_idempotent() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    assert record is not None
    assert record.state == DemoExecutionState.DEMO_ORDER_ACKNOWLEDGED
    assert record.accepted_quantity == 0
    assert record.fills == []
    duplicate = demo.submit_candidate(candidate, preview, classification, snapshot)
    assert duplicate is not None and duplicate.id == record.id
    assert len(client.orders) == 1


def test_rest_poll_failure_after_ack_preserves_acknowledged_state() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()

    def unavailable(*args, **kwargs):
        raise TimeoutError("temporary REST timeout")

    client.get_order_history = unavailable
    record = demo.submit_candidate(candidate, preview, classification, snapshot)

    assert record is not None
    assert record.state == DemoExecutionState.DEMO_ORDER_ACKNOWLEDGED
    assert len(client.orders) == 1
    assert demo.orders_accepted == 1
    assert demo.orders_rejected == 0


@pytest.mark.parametrize("source", ["history", "execution", "position"])
def test_immediate_full_fill_is_reconciled_before_first_ws_event(source: str) -> None:
    repo = MemoryRepository()
    client = ImmediateFillClient(source)
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()

    record = demo.submit_candidate(candidate, preview, classification, snapshot)

    assert record is not None
    assert record.state == DemoExecutionState.DEMO_POSITION_OPEN
    assert record.accepted_quantity == record.requested_quantity
    assert record.average_fill_price == Decimal("65000")
    assert record.protection_confirmed is True
    assert ("DEMO_ORDER_ACKNOWLEDGED", "DEMO_ORDER_ACKNOWLEDGED") in repo.saved_events
    assert any(event == "DEMO_FULLY_FILLED" for event, _ in repo.saved_events)
    assert any(event == "DEMO_POSITION_OPEN" for event, _ in repo.saved_events)


def test_stale_ws_position_without_protection_does_not_close_verified_position() -> None:
    repo = MemoryRepository()
    client = ImmediateFillClient("history")
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    assert record is not None and record.state == DemoExecutionState.DEMO_POSITION_OPEN

    demo.handle_private_event({
        "topic": "position",
        "data": [{"symbol": "BTCUSDT", "size": "0.010"}],
    })

    saved = repo.get_demo_execution(str(candidate.id))
    assert saved.state == DemoExecutionState.DEMO_POSITION_OPEN
    assert len(client.orders) == 1


def test_terminal_closed_state_is_not_downgraded_by_replayed_close_history() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = DemoExecutionRecord(
        candidate_id=candidate.id, risk_decision_id=1, run_id="test-run",
        order_link_id="entry-link", order_id="entry-id",
        close_order_link_id="close-link", close_order_id="close-id",
        state=DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE,
        symbol=Symbol.BTCUSDT, side=Side.BUY,
        requested_quantity=Decimal("0.010"),
        accepted_quantity=Decimal("0.010"),
        average_fill_price=Decimal("65000"),
        failure_reason="local position-open state timeout",
    )
    repo.records[str(candidate.id)] = record

    demo._apply_order_update(record, {
        "orderId": "close-id", "orderLinkId": "close-link",
        "orderStatus": "Filled",
    })

    assert record.state == DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE


def test_timeout_cleanup_persists_fills_pnl_and_closed_after_failure() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = DemoExecutionService(
        demo_settings(demo_canary_enabled=True), repo, client,
        run_id="demo-test-run",
    )
    candidate, _, _, _ = candidate_bundle()
    now = datetime.now(timezone.utc)
    record = DemoExecutionRecord(
        candidate_id=candidate.id, risk_decision_id=7, run_id="demo-test-run",
        order_link_id="entry-link", order_id="entry-id",
        close_order_link_id="close-link", close_order_id="close-id",
        state=DemoExecutionState.DEMO_POSITION_OPEN,
        symbol=Symbol.BTCUSDT, side=Side.BUY,
        requested_quantity=Decimal("0.001"),
        accepted_quantity=Decimal("0.001"),
        average_fill_price=Decimal("64021.8"),
        fills=[DemoFill(
            execution_id="entry-fill", order_id="entry-id",
            quantity=Decimal("0.001"), price=Decimal("64021.8"),
            fee=Decimal("0.03521199"), fee_currency="USDT", executed_at=now,
        )],
        close_fills=[DemoFill(
            execution_id="close-fill", order_id="close-id",
            quantity=Decimal("0.001"), price=Decimal("64020"),
            fee=Decimal("0.035211"), fee_currency="USDT", executed_at=now,
        )],
        average_close_price=Decimal("64020"),
        exchange_fees=Decimal("0.07042299"),
        paper_shadow_pnl=Decimal("-0.07222299"),
    )
    repo.records[str(candidate.id)] = record

    cleaned = demo.request_canary_failure_cleanup(
        str(record.id), "local position-open state timeout"
    )

    assert cleaned is not None
    assert cleaned.state == DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE
    assert cleaned.failure_reason == "local position-open state timeout"
    assert cleaned.cleanup_result == "remote position flat and bot-owned orders zero"
    assert cleaned.realized_exchange_pnl == Decimal("-0.07222299")
    assert len(cleaned.fills) == 1 and len(cleaned.close_fills) == 1
    assert len(client.orders) == 0


def test_entry_is_never_submitted_without_durable_reservation() -> None:
    client = FakeDemoClient()
    demo = service(client, FailingReservationRepository())
    candidate, classification, preview, snapshot = candidate_bundle()
    assert demo.submit_candidate(candidate, preview, classification, snapshot) is None
    assert client.orders == []


def test_entry_is_never_submitted_without_durable_risk_decision() -> None:
    client = FakeDemoClient()
    demo = service(client, MemoryRepository())
    candidate, classification, preview, snapshot = candidate_bundle()
    preview.risk_decision_id = None
    assert demo.submit_candidate(candidate, preview, classification, snapshot) is None
    assert client.orders == []


def test_historical_paper_candidate_cannot_submit_demo_order() -> None:
    client = FakeDemoClient()
    demo = service(client, MemoryRepository())
    candidate, classification, preview, snapshot = candidate_bundle()
    candidate.execution_environment = ExecutionEnvironment.PAPER

    assert demo.submit_candidate(candidate, preview, classification, snapshot) is None
    assert demo.last_error == "candidate execution environment is not BYBIT_DEMO"
    assert client.orders == []


def test_partial_fill_and_duplicate_websocket_event() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    event = {"topic": "execution", "data": [{
        "execId": "exec-1", "orderId": record.order_id,
        "orderLinkId": record.order_link_id, "execQty": "0.004",
        "execPrice": "65001", "execFee": "0.13", "execTime": "1783900000000",
    }]}
    demo.handle_private_event(event)
    demo.handle_private_event(event)
    demo.handle_private_event({"topic": "order", "data": [{
        "orderId": record.order_id, "orderLinkId": record.order_link_id,
        "orderStatus": "PartiallyFilled",
    }]})
    saved = repo.get_demo_execution(str(candidate.id))
    assert saved.accepted_quantity == Decimal("0.004")
    assert len(saved.fills) == 1
    assert saved.state == DemoExecutionState.DEMO_PARTIALLY_FILLED


def test_fill_installs_and_verifies_exchange_protection() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    client.positions = [{
        "symbol": "BTCUSDT", "size": "0.010", "side": "Buy",
        "avgPrice": "65000", "leverage": "1", "positionIdx": 0,
    }]
    demo.handle_private_event({"topic": "order", "data": [{
        "orderId": record.order_id, "orderLinkId": record.order_link_id,
        "orderStatus": "Filled", "cumExecQty": "0.010", "avgPrice": "65000",
    }]})
    saved = repo.get_demo_execution(str(candidate.id))
    assert saved.state == DemoExecutionState.DEMO_POSITION_OPEN
    assert saved.protection_confirmed is True
    assert saved.take_profit and saved.stop_loss


def test_restart_rest_reconciliation_recovers_filled_entry() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    client.history = [{
        "orderId": record.order_id, "orderLinkId": record.order_link_id,
        "orderStatus": "Filled", "cumExecQty": "0.010", "avgPrice": "65000",
    }]
    client.executions = [{
        "execId": "rest-exec", "orderId": record.order_id,
        "orderLinkId": record.order_link_id, "execQty": "0.010",
        "execPrice": "65000", "execFee": "0.3", "execTime": "1783900000000",
    }]
    client.positions = [{
        "symbol": "BTCUSDT", "size": "0.010", "side": "Buy",
        "avgPrice": "65000", "leverage": "1", "positionIdx": 0,
    }]
    recovered = service(client, repo)
    recovered.reconcile()
    saved = repo.get_demo_execution(str(candidate.id))
    assert saved.state == DemoExecutionState.DEMO_POSITION_OPEN
    assert saved.protection_confirmed is True
    assert len(saved.fills) == 1


def test_unprotected_fill_causes_reduce_only_close_and_kill_switch() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    client.protection_ok = False
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    client.positions = [{
        "symbol": "BTCUSDT", "size": "0.010", "side": "Buy",
        "avgPrice": "65000", "leverage": "1", "positionIdx": 0,
    }]
    demo.handle_private_event({"topic": "order", "data": [{
        "orderId": record.order_id, "orderLinkId": record.order_link_id,
        "orderStatus": "Filled", "cumExecQty": "0.010", "avgPrice": "65000",
    }]})
    assert demo.kill_switch_active is True
    assert client.orders[-1]["reduceOnly"] == "true"
    assert repo.get_demo_execution(str(candidate.id)).state == DemoExecutionState.DEMO_CLOSING


def test_cleanup_touches_only_bot_owned_orders_and_positions() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    record.state = DemoExecutionState.DEMO_POSITION_OPEN
    record.accepted_quantity = Decimal("0.010")
    repo.save_demo_execution(record, event_type="TEST")
    client.open_orders = [
        {"symbol": "BTCUSDT", "orderId": "bot", "orderLinkId": record.order_link_id},
        {"symbol": "ETHUSDT", "orderId": "manual", "orderLinkId": "manual-order"},
    ]
    client.positions = [
        {"symbol": "BTCUSDT", "size": "0.010"},
        {"symbol": "ETHUSDT", "size": "1"},
    ]
    result = demo.cleanup_bot_owned()
    assert result == {"orders_cancelled": 1, "positions_closed": 1}
    assert client.cancelled == [(Symbol.BTCUSDT, "bot")]
    assert all(order.get("symbol") != "ETHUSDT" for order in client.orders)


def test_duplicate_close_submission_is_prevented() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    record.state = DemoExecutionState.DEMO_POSITION_OPEN
    record.accepted_quantity = Decimal("0.010")
    repo.save_demo_execution(record, event_type="TEST")
    client.positions = [{"symbol": "BTCUSDT", "size": "0.010"}]
    demo.cleanup_bot_owned()
    demo.cleanup_bot_owned()
    reduce_only = [item for item in client.orders if item.get("reduceOnly") == "true"]
    assert len(reduce_only) == 1


def test_controlled_canary_close_uses_one_idempotent_reduce_only_order() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = DemoExecutionService(
        demo_settings(demo_canary_enabled=True), repo, client,
        run_id="demo-canary-test",
    )
    record = DemoExecutionRecord(
        candidate_id=uuid4(),
        risk_decision_id=1,
        run_id="demo-canary-test",
        order_link_id="bybot-canary-entry",
        order_id="entry-order",
        state=DemoExecutionState.DEMO_POSITION_OPEN,
        symbol=Symbol.BTCUSDT,
        side=Side.BUY,
        requested_quantity=Decimal("0.010"),
        accepted_quantity=Decimal("0.010"),
        average_fill_price=Decimal("65000"),
        protection_confirmed=True,
    )
    repo.records[str(record.candidate_id)] = record
    client.positions = [{
        "symbol": "BTCUSDT", "size": "0.010", "leverage": "1",
        "positionIdx": 0, "takeProfit": "65650", "stopLoss": "64675",
    }]

    first = demo.request_canary_close(str(record.id))
    second = demo.request_canary_close(str(record.id))

    assert first is not None and second is not None
    reduce_only = [item for item in client.orders if item.get("reduceOnly") == "true"]
    assert len(reduce_only) == 1
    assert reduce_only[0]["symbol"] == "BTCUSDT"
    assert reduce_only[0]["qty"] == "0.01"
    assert first.close_order_link_id == second.close_order_link_id


def test_controlled_canary_entry_uses_production_demo_service_once(tmp_path) -> None:
    settings = demo_settings(demo_canary_enabled=True, demo_run_id="canary-entry-test")
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'canary.db'}")
    client = FakeDemoClient()
    demo = DemoExecutionService(
        settings, repository, client, run_id="canary-entry-test"
    )
    news_service = SimpleNamespace(
        items=[], filtered_items=[], classifications=[]
    )
    service_under_test = SignalCandidateService(
        settings,
        news_service,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(equity=10_000),
        repository,
        demo,
    )
    snapshot = MarketSnapshot(
        symbol=Symbol.BTCUSDT,
        timestamp=datetime.now(timezone.utc),
        last_price=100,
        bid_price=99.99,
        ask_price=100.01,
        price_change_1m_pct=0.2,
        simple_trend=SimpleTrend.BULLISH,
        trend_score=0.5,
        volatility_pct=0.1,
        liquidity_ok=True,
    )

    budget = Decimal("20")
    plan = demo.plan_canary_order(
        Symbol.BTCUSDT, budget, Decimal(str(snapshot.ask_price))
    )
    first = service_under_test.execute_demo_canary(
        Symbol.BTCUSDT,
        budget,
        snapshot,
        expected_rules_fingerprint=plan.rules_fingerprint,
    )
    second = service_under_test.execute_demo_canary(
        Symbol.BTCUSDT,
        budget,
        snapshot,
        expected_rules_fingerprint=plan.rules_fingerprint,
    )

    assert first.demo_execution is not None
    assert first.demo_execution["execution_environment"] == "BYBIT_DEMO"
    assert Decimal(str(first.demo_execution["requested_quantity"])) * Decimal(
        str(snapshot.ask_price)
    ) <= Decimal("20")
    assert first.risk_preview.risk_decision_id is not None
    assert second.demo_execution is not None
    assert second.demo_execution["id"] == first.demo_execution["id"]
    assert len(client.orders) == 1


def test_canary_fails_before_persistence_when_exchange_minimum_exceeds_cap(
    tmp_path,
) -> None:
    settings = demo_settings(demo_canary_enabled=True, demo_run_id="canary-min-test")
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'minimum.db'}")
    client = FakeDemoClient()
    demo = DemoExecutionService(settings, repository, client, run_id="canary-min-test")
    service_under_test = SignalCandidateService(
        settings,
        SimpleNamespace(items=[], filtered_items=[], classifications=[]),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(equity=10_000),
        repository,
        demo,
    )
    snapshot = MarketSnapshot(
        symbol=Symbol.BTCUSDT,
        timestamp=datetime.now(timezone.utc),
        last_price=65_000,
        bid_price=64_999.9,
        ask_price=65_000.1,
        simple_trend=SimpleTrend.BULLISH,
        trend_score=0.5,
        volatility_pct=0.1,
        liquidity_ok=True,
    )

    with pytest.raises(DemoSafetyError, match="exchange minimum"):
        service_under_test.execute_demo_canary(
            Symbol.BTCUSDT,
            Decimal("20"),
            snapshot,
            expected_rules_fingerprint="0" * 64,
        )

    assert client.orders == []
    assert repository.load_signal_results(ExecutionEnvironment.BYBIT_DEMO) == []
    assert repository.load_demo_executions() == []
    restored_news, restored_classifications = repository.load_news()
    assert restored_news == []
    assert restored_classifications == []


class MutableCanaryRulesClient(FakeDemoClient):
    def __init__(self) -> None:
        super().__init__()
        self.rules = instrument()

    def get_instrument(self, symbol):
        return self.rules


def _canary_signal_service(tmp_path, client, *, run_id: str):
    settings = demo_settings(demo_canary_enabled=True, demo_run_id=run_id)
    repository = PersistenceRepository(f"sqlite:///{tmp_path / (run_id + '.db')}")
    demo = DemoExecutionService(settings, repository, client, run_id=run_id)
    candidate_service = SignalCandidateService(
        settings,
        SimpleNamespace(items=[], filtered_items=[], classifications=[]),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(equity=10_000),
        repository,
        demo,
    )
    return candidate_service, demo, repository


def _canary_snapshot(price: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=Symbol.BTCUSDT,
        timestamp=datetime.now(timezone.utc),
        last_price=price,
        bid_price=price - 0.1,
        ask_price=price,
        price_change_1m_pct=0.2,
        simple_trend=SimpleTrend.BULLISH,
        trend_score=0.5,
        volatility_pct=0.1,
        liquidity_ok=True,
    )


def _assert_no_canary_side_effects(repository, client) -> None:
    assert client.orders == []
    assert repository.load_demo_executions() == []
    assert repository.load_signal_results(ExecutionEnvironment.BYBIT_DEMO) == []
    news, classifications = repository.load_news()
    assert news == []
    assert classifications == []


def test_canary_rule_change_is_rejected_before_durable_side_effects(tmp_path) -> None:
    client = MutableCanaryRulesClient()
    candidate_service, demo, repository = _canary_signal_service(
        tmp_path, client, run_id="canary-rule-race"
    )
    initial = demo.plan_canary_order(
        Symbol.BTCUSDT, Decimal("75"), Decimal("62800")
    )
    client.rules = InstrumentRules(
        **{
            **client.rules.__dict__,
            "tick_size": Decimal("0.01"),
        }
    )

    with pytest.raises(ValueError, match="instrument rules changed"):
        candidate_service.execute_demo_canary(
            Symbol.BTCUSDT,
            Decimal("75"),
            _canary_snapshot(62800),
            expected_rules_fingerprint=initial.rules_fingerprint,
        )

    _assert_no_canary_side_effects(repository, client)


def test_canary_price_change_beyond_budget_has_no_durable_side_effects(tmp_path) -> None:
    client = MutableCanaryRulesClient()
    candidate_service, demo, repository = _canary_signal_service(
        tmp_path, client, run_id="canary-price-race"
    )
    initial = demo.plan_canary_order(
        Symbol.BTCUSDT, Decimal("75"), Decimal("62800")
    )

    with pytest.raises(DemoSafetyError, match="maximum canary budget"):
        candidate_service.execute_demo_canary(
            Symbol.BTCUSDT,
            Decimal("75"),
            _canary_snapshot(72000),
            expected_rules_fingerprint=initial.rules_fingerprint,
        )

    _assert_no_canary_side_effects(repository, client)


def test_canary_submits_exact_minimum_quantity_not_budget_quantity(tmp_path) -> None:
    client = MutableCanaryRulesClient()
    candidate_service, demo, _ = _canary_signal_service(
        tmp_path, client, run_id="canary-exact-minimum"
    )
    initial = demo.plan_canary_order(
        Symbol.BTCUSDT, Decimal("75"), Decimal("62800")
    )

    result = candidate_service.execute_demo_canary(
        Symbol.BTCUSDT,
        Decimal("75"),
        _canary_snapshot(62800),
        expected_rules_fingerprint=initial.rules_fingerprint,
    )

    assert result.execution_attempted is True
    assert len(client.orders) == 1
    assert client.orders[0]["qty"] == "0.001"
    assert Decimal(client.orders[0]["qty"]) * Decimal("62800") < Decimal("75")


def test_reconciliation_fails_closed_on_remote_quantity_mismatch() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    record.state = DemoExecutionState.DEMO_POSITION_OPEN
    record.accepted_quantity = Decimal("0.010")
    record.protection_confirmed = True
    repo.save_demo_execution(record, event_type="TEST")
    client.positions = [{
        "symbol": "BTCUSDT", "size": "0.020", "takeProfit": "65650",
        "stopLoss": "64675", "leverage": "1", "positionIdx": 0,
    }]
    demo.reconcile()
    assert demo.kill_switch_active is True
    assert "quantity mismatch" in demo.kill_switch_reasons[-1]


def test_reconciliation_uses_usdt_scope_and_keeps_unrelated_visibility() -> None:
    client = FakeDemoClient()
    client.open_orders = [
        {"symbol": "BTCUSDT", "orderId": "old-bot", "orderLinkId": "bybot-old-e-1"},
        {"symbol": "ETHUSDT", "orderId": "manual", "orderLinkId": "manual-order"},
    ]
    demo = service(client)

    result = demo.reconcile()

    assert client.open_order_scopes == [(None, "USDT")]
    assert client.history_scopes == [(None, "USDT")]
    assert client.execution_scopes == [(None, "USDT")]
    assert client.closed_pnl_scopes == [(None, "USDT")]
    assert client.position_scopes == [(None, "USDT")]
    assert result["bot_owned_open_orders"] == 1
    assert result["unrelated_open_orders"] == 1
    assert demo.as_status()["unrelated_open_orders"] == 1
    assert client.orders == []


def test_order_link_id_is_stable_and_purpose_specific() -> None:
    candidate_id = uuid4()
    first = deterministic_order_link_id("bybot", candidate_id, "entry")
    assert first == deterministic_order_link_id("bybot", candidate_id, "entry")
    assert first != deterministic_order_link_id("bybot", candidate_id, "close")
    assert len(first) <= 36


def test_demo_execution_and_kill_switch_survive_repository_restart(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    repository = PersistenceRepository(url)
    candidate, classification, preview, snapshot = candidate_bundle()
    result = SignalDryRunResult(candidate=candidate, risk_preview=preview)
    repository.save_signal_result(result)
    record = service(FakeDemoClient(), repository).submit_candidate(
        candidate, preview, classification, snapshot
    )
    assert record is not None
    repository.save_demo_kill_switch(True, ["test incident"])

    restored = PersistenceRepository(url)
    loaded = restored.get_demo_execution(str(candidate.id))
    assert loaded is not None and loaded.id == record.id
    assert loaded.state == DemoExecutionState.DEMO_ORDER_ACKNOWLEDGED
    assert restored.load_demo_kill_switch()["reasons"] == ["test incident"]


def test_entry_replay_after_close_started_cannot_regress_state() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    record.state = DemoExecutionState.DEMO_CLOSING
    record.close_order_link_id = "bybot-close-existing"
    repo.save_demo_execution(record, event_type="CLOSE_SUBMITTING")

    demo._apply_order_update(record, {
        "orderId": record.order_id, "orderLinkId": record.order_link_id,
        "orderStatus": "Filled", "cumExecQty": "0.010", "avgPrice": "65000",
    })

    assert record.state == DemoExecutionState.DEMO_CLOSING
    assert record.close_order_link_id == "bybot-close-existing"


def test_close_attribution_rejects_pre_entry_and_globally_used_identity() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, _, _, _ = candidate_bundle()
    entry_at = datetime.now(timezone.utc)
    current = DemoExecutionRecord(
        candidate_id=candidate.id, run_id=demo.run_id, order_link_id="new-entry",
        state=DemoExecutionState.DEMO_POSITION_OPEN, symbol=Symbol.BTCUSDT,
        side=Side.BUY, requested_quantity=Decimal("0.001"),
        accepted_quantity=Decimal("0.001"), average_fill_price=Decimal("100"),
        fills=[DemoFill(
            execution_id="new-entry-exec", order_id="new-entry-order",
            quantity=Decimal("0.001"), price=Decimal("100"),
            executed_at=entry_at,
        )],
    )
    old = current.model_copy(deep=True, update={
        "id": uuid4(), "candidate_id": uuid4(), "order_link_id": "old-entry",
        "state": DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
        "close_order_id": "used-close",
        "close_fills": [DemoFill(
            execution_id="used-exec", order_id="used-close",
            quantity=Decimal("0.001"), price=Decimal("101"),
            executed_at=entry_at + timedelta(seconds=1),
        )],
    })
    repo.records = {
        str(current.candidate_id): current,
        str(old.candidate_id): old,
    }

    before_entry = {
        "symbol": "BTCUSDT", "side": "Sell", "orderId": "historical",
        "execId": "historical-exec", "execQty": "0.001", "reduceOnly": True,
        "execTime": str(int(entry_at.timestamp() * 1000) - 1),
    }
    already_used = {
        "symbol": "BTCUSDT", "side": "Sell", "orderId": "used-close",
        "execId": "used-exec", "execQty": "0.001", "reduceOnly": True,
        "execTime": str(int(entry_at.timestamp() * 1000) + 1000),
    }
    valid = {
        "symbol": "BTCUSDT", "side": "Sell", "orderId": "new-close",
        "execId": "new-close-exec", "execQty": "0.001", "reduceOnly": True,
        "execTime": str(int(entry_at.timestamp() * 1000) + 2000),
    }

    assert demo._attributable_close_record(before_entry) is None
    assert demo._attributable_close_record(already_used) is None
    assert demo._attributable_close_record(valid).id == current.id


def test_cached_canary_polling_performs_no_exchange_io() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, _, _, _ = candidate_bundle()
    record = DemoExecutionRecord(
        candidate_id=candidate.id, run_id=demo.run_id, order_link_id="entry",
        state=DemoExecutionState.DEMO_POSITION_OPEN, symbol=Symbol.BTCUSDT,
        side=Side.BUY, requested_quantity=Decimal("0.001"),
    )
    repo.records[str(candidate.id)] = record
    client.get_positions = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("polling performed exchange I/O")
    )

    started = time.perf_counter()
    payload = demo.canary_cached_status(str(record.id))

    assert time.perf_counter() - started < 0.1
    assert payload["execution"]["id"] == str(record.id)
    assert payload["durable_cached_state"] is True


def test_zero_position_before_protection_never_calls_trading_stop() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    candidate, classification, preview, snapshot = candidate_bundle()
    record = demo.submit_candidate(candidate, preview, classification, snapshot)
    record.average_fill_price = Decimal("65000")
    record.accepted_quantity = Decimal("0.010")
    record.state = DemoExecutionState.DEMO_FULLY_FILLED
    calls = []
    client.set_trading_stop = lambda *args: calls.append(args)

    demo._install_protection(record)

    assert calls == []
    assert demo.kill_switch_active is False
    assert repo.saved_events[-1][0] == "PROTECTION_SKIPPED_POSITION_FLAT"


def test_stale_private_event_after_resume_watermark_is_discarded() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = service(client, repo)
    demo._discard_ws_before_ms = 2000
    demo.handle_private_event({
        "topic": "order", "creationTime": "1000",
        "data": [{"orderId": "old", "updatedTime": "1000", "orderStatus": "Filled"}],
    })
    assert repo.events == set()


def test_direct_cleanup_cancels_only_owned_protection_and_closes_reduce_only() -> None:
    repo, client = MemoryRepository(), FakeDemoClient()
    demo = DemoExecutionService(
        demo_settings(demo_canary_enabled=True),
        repo, client, run_id="demo-test-run",
    )
    candidate, _, _, _ = candidate_bundle()
    record = DemoExecutionRecord(
        candidate_id=candidate.id, risk_decision_id=1, run_id=demo.run_id,
        order_link_id="entry", order_id="entry-id",
        state=DemoExecutionState.DEMO_POSITION_OPEN,
        symbol=Symbol.BTCUSDT, side=Side.BUY,
        requested_quantity=Decimal("0.001"), accepted_quantity=Decimal("0.001"),
        average_fill_price=Decimal("65000"), take_profit=Decimal("65650"),
        stop_loss=Decimal("64675"), protection_confirmed=True,
    )
    repo.records[str(candidate.id)] = record
    client.positions = [{
        "symbol": "BTCUSDT", "size": "0.001", "side": "Buy",
        "takeProfit": "65650", "stopLoss": "64675", "leverage": "1",
        "positionIdx": 0,
    }]
    client.open_orders = [{
        "symbol": "BTCUSDT", "orderId": "tp", "orderLinkId": "",
        "side": "Sell", "qty": "0.001", "reduceOnly": True,
        "closeOnTrigger": True, "stopOrderType": "TakeProfit",
        "triggerPrice": "65650",
    }]

    demo.direct_cleanup_execution(str(record.id), "restart failed")

    assert client.cancelled == [(Symbol.BTCUSDT, "tp")]
    assert len(client.orders) == 1
    assert client.orders[0]["reduceOnly"] == "true"
    assert client.orders[0]["qty"] == "0.001"
