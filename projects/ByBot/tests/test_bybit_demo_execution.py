from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
    DemoExecutionState,
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
from app.signals.service import risk_capital_for_execution


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
        self.positions = []
        self.open_orders = []
        self.history = []
        self.executions = []
        self.closed_pnl = []
        self.protection_ok = True

    def verify_credentials(self):
        return True

    def get_instrument(self, symbol):
        return instrument()

    def get_positions(self, symbol=None):
        return list(self.positions)

    def get_open_orders(self, symbol=None):
        return list(self.open_orders)

    def get_order_history(self, symbol=None):
        return list(self.history)

    def get_executions(self, symbol=None):
        return list(self.executions)

    def get_closed_pnl(self, symbol=None):
        return list(self.closed_pnl)

    def set_leverage(self, symbol, leverage):
        assert leverage == Decimal("1")
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


def candidate_bundle():
    news_id = uuid4()
    candidate = NewsSignalCandidate(
        news_id=news_id, symbol=Symbol.BTCUSDT,
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


def test_test_mode_and_live_configuration_are_rejected() -> None:
    with pytest.raises(ValidationError):
        demo_settings(test_mode=True)
    with pytest.raises(ValidationError):
        demo_settings(bybit_live_trading_enabled=True)


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
    assert record.state == DemoExecutionState.DEMO_ACCEPTED
    assert record.accepted_quantity == 0
    assert record.fills == []
    duplicate = demo.submit_candidate(candidate, preview, classification, snapshot)
    assert duplicate is not None and duplicate.id == record.id
    assert len(client.orders) == 1


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
    assert loaded.state == DemoExecutionState.DEMO_ACCEPTED
    assert restored.load_demo_kill_switch()["reasons"] == ["test incident"]
