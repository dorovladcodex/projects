from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.bybit.demo import DemoExecutionService
from app.config import Settings
from app.models import DemoExecutionRecord, DemoExecutionState, DemoFill, Side, Symbol
from app.v2.analytics import _trade_row
from app.v2.market import BybitPublicWebSocketEngine, RollingFeatureEngine
from app.v2.runtime import _news_classification_reason, _news_skip_reason
from tests.test_bybit_demo_execution import FakeDemoClient, MemoryRepository, demo_settings
from tests.test_v2_runtime_observability import runtime
from tests.test_v2_system import feature


def _closing_record(quantity: str = "0.010") -> DemoExecutionRecord:
    entry_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    candidate_id = uuid4()
    return DemoExecutionRecord(
        candidate_id=candidate_id,
        run_id="review-run",
        order_link_id="bybot-review-entry",
        order_id="entry-order",
        close_order_id="close-order",
        state=DemoExecutionState.DEMO_CLOSING,
        symbol=Symbol.BTCUSDT,
        side=Side.BUY,
        requested_quantity=Decimal(quantity),
        accepted_quantity=Decimal(quantity),
        average_fill_price=Decimal("65000"),
        stop_loss=Decimal("64000"),
        take_profit=Decimal("67000"),
        protection_confirmed=True,
        fills=[DemoFill(
            execution_id="entry-exec",
            order_id="entry-order",
            quantity=Decimal(quantity),
            price=Decimal("65000"),
            fee=Decimal("0.10"),
            executed_at=entry_at,
        )],
        created_at=entry_at - timedelta(seconds=1),
    )


def _close_payload(record: DemoExecutionRecord, quantity: str = "0.010") -> tuple[dict, dict]:
    close_ms = int((record.fills[0].executed_at + timedelta(minutes=1)).timestamp() * 1000)
    order = {
        "symbol": record.symbol.value,
        "orderId": "close-order",
        "orderStatus": "Filled",
        "orderLinkId": "",
        "side": "Sell",
        "reduceOnly": True,
        "closeOnTrigger": True,
        "stopOrderType": "StopLoss",
        "cumExecQty": quantity,
        "qty": quantity,
        "avgPrice": "64900",
        "updatedTime": str(close_ms),
    }
    fill = {
        "symbol": record.symbol.value,
        "orderId": "close-order",
        "orderLinkId": "",
        "execId": "close-exec",
        "execQty": quantity,
        "execPrice": "64900",
        "execFee": "0.10",
        "feeCurrency": "USDT",
        "execTime": str(close_ms),
        "closedSize": quantity,
        "side": "Sell",
        "reduceOnly": True,
        "stopOrderType": "StopLoss",
    }
    return order, fill


def test_flat_fully_attributed_closing_execution_finalizes_exactly_once() -> None:
    repository = MemoryRepository()
    record = _closing_record()
    repository.records[str(record.candidate_id)] = record
    client = FakeDemoClient()
    order, fill = _close_payload(record)
    client.history = [order]
    client.executions = [fill]
    service = DemoExecutionService(
        demo_settings(), repository, client, run_id=record.run_id
    )

    result = service._reconcile_execution_rest(record)

    assert result.state == DemoExecutionState.DEMO_CLOSED
    assert result.cleanup_result == "remote position flat and bot-owned orders zero"
    assert sum(item.quantity for item in result.close_fills) == record.accepted_quantity
    assert repository.saved_events.count(("REST_FLAT_CLOSE_FINALIZED", "DEMO_CLOSED")) == 1
    service._reconcile_execution_rest(result)
    assert repository.saved_events.count(("REST_FLAT_CLOSE_FINALIZED", "DEMO_CLOSED")) == 1


def test_flat_closing_execution_does_not_finalize_with_incomplete_attribution() -> None:
    repository = MemoryRepository()
    record = _closing_record()
    repository.records[str(record.candidate_id)] = record
    client = FakeDemoClient()
    order, fill = _close_payload(record, "0.005")
    client.history = [order]
    client.executions = [fill]
    service = DemoExecutionService(
        demo_settings(), repository, client, run_id=record.run_id
    )

    result = service._reconcile_execution_rest(record)

    assert result.state == DemoExecutionState.DEMO_CLOSING
    assert "REST_FLAT_CLOSE_FINALIZED" not in [event for event, _ in repository.saved_events]


def test_fill_before_ack_is_supported_without_negative_latency_failure() -> None:
    now = datetime.now(timezone.utc)
    row = _trade_row({
        "id": "execution", "run_id": "run", "state": "DEMO_CLOSED",
        "local_submit_started_at": now.isoformat(),
        "local_fill_received_at": (now + timedelta(milliseconds=20)).isoformat(),
        "local_ack_received_at": (now + timedelta(milliseconds=50)).isoformat(),
        "execution_stage_durations_ms": {"exchange_submit": 50.0},
    })
    assert row["fill_before_ack"] is True
    assert row["ack_to_first_fill_ms"] is None
    assert row["latency_validation_errors"] == []
    assert "fill_before_ack_supported" in row["latency_diagnostic_codes"]


def test_naive_timestamp_is_reported_as_incompatible_clock_input() -> None:
    now = datetime.now(timezone.utc)
    row = _trade_row({
        "id": "execution", "run_id": "run", "state": "DEMO_CLOSED",
        "signal_created_at": now.replace(tzinfo=None).isoformat(),
        "local_submit_started_at": now.isoformat(),
    })
    assert "invalid_utc_timestamp:signal_to_order" in row["latency_validation_errors"]


def test_healthy_subscribed_liquidation_stream_with_zero_events_is_valid() -> None:
    settings = Settings(_env_file=None)
    engine = RollingFeatureEngine(settings)
    symbol = Symbol.BTCUSDT
    now = datetime.now(timezone.utc)
    engine.ingest_ticker(symbol, {"lastPrice": "65000", "volume24h": "1"}, now)
    engine.ingest_orderbook(symbol, [["64999", "1"]], [["65001", "1"]], now)
    engine.ingest_trade(symbol, Decimal("65000"), Decimal("0.1"), "Buy", now)
    engine.mark_transport_connected(True, at=now)
    engine.mark_liquidation_subscribed((symbol,), at=now)

    snapshot = engine.snapshot(symbol, now=now)

    assert snapshot is not None
    assert snapshot.liquidation_feed_initialized is True
    assert snapshot.liquidation_feed_available is True
    assert snapshot.liquidation_event_count_5m == 0
    assert snapshot.liquidation_notional_5m == 0
    assert snapshot.liquidation_data_age_seconds is None


def test_liquidation_subscription_ack_initializes_all_requested_symbols() -> None:
    features = RollingFeatureEngine(Settings(_env_file=None))
    websocket = BybitPublicWebSocketEngine(Settings(_env_file=None), features)
    websocket._requested_symbols = (Symbol.BTCUSDT, Symbol.ETHUSDT)
    websocket.handle_message({"op": "subscribe", "success": True})
    assert set(features.liquidation_subscriptions) == {Symbol.BTCUSDT, Symbol.ETHUSDT}
    assert features.source_states["liquidations"].subscribed is True


def test_liquidation_subscription_reports_unsupported_symbol_independently() -> None:
    features = RollingFeatureEngine(Settings(_env_file=None))
    websocket = BybitPublicWebSocketEngine(Settings(_env_file=None), features)
    websocket._requested_symbols = (Symbol.BTCUSDT, Symbol.ETHUSDT)
    websocket.handle_message({
        "op": "subscribe", "success": True,
        "failTopics": ["allLiquidation.ETHUSDT"],
    })
    assert Symbol.BTCUSDT in features.liquidation_subscriptions
    assert Symbol.ETHUSDT not in features.liquidation_subscriptions
    assert Symbol.ETHUSDT in features.unsupported_liquidation_symbols


@pytest.mark.parametrize(
    "code,expected",
    [
        ("HOURLY_REQUEST_BUDGET", "classifier_budget_rejected"),
        ("DAILY_REQUEST_BUDGET", "classifier_budget_rejected"),
        ("DAILY_TOKEN_BUDGET", "classifier_budget_rejected"),
        ("CIRCUIT_OPEN", "classifier_circuit_breaker_open"),
        ("TIMEOUT", "classifier_failed:timeout"),
    ],
)
def test_news_classifier_rejection_reasons_are_explicit(code: str, expected: str) -> None:
    classification = SimpleNamespace(
        error_code=code, cache_hit=False, provider_name="codex-cli"
    )
    assert _news_classification_reason(classification, False, {}, {}) == expected


@pytest.mark.parametrize("reason", ["missing_keywords", "low_importance", "duplicate", "old_news"])
def test_news_prefilter_rejection_reasons_are_preserved(reason: str) -> None:
    assert _news_skip_reason(
        accepted=False, filter_reason=reason, classification=None,
        before={}, after={},
    ) == reason


def test_news_cache_hit_is_distinct_from_provider_request() -> None:
    classification = SimpleNamespace(
        error_code=None, cache_hit=True, provider_name="codex-cli"
    )
    assert _news_classification_reason(classification, False, {}, {}) == "classifier_cache_hit"


def test_v2_status_is_cached_and_does_not_execute_preflight_or_database_io(tmp_path, monkeypatch) -> None:
    app, repository, _ = runtime(tmp_path, (Symbol.BTCUSDT,))
    expected_run = app.status()["run_id"]
    monkeypatch.setattr(
        app.execution, "safety_preflight",
        lambda **_: (_ for _ in ()).throw(AssertionError("preflight called")),
    )
    monkeypatch.setattr(
        repository, "load_demo_executions",
        lambda: (_ for _ in ()).throw(AssertionError("database called")),
    )
    started = perf_counter()
    payload = app.status()
    elapsed = perf_counter() - started
    assert payload["run_id"] == expected_run
    assert elapsed < 0.05


def test_signal_taxonomy_separates_evaluations_raw_candidates_and_admissions(tmp_path) -> None:
    app, _, _ = runtime(tmp_path, (Symbol.BTCUSDT,))
    strategy = next(
        item for item in app.strategies
        if item.name.value == "VolumeBreakoutStrategy"
    )
    first = strategy.evaluate(feature(Symbol.BTCUSDT))
    second = first.model_copy(update={"id": uuid4()})
    app._admit(first)
    duplicate = app._admit(second)
    metrics = app._signal_metrics_snapshot()
    assert metrics["raw_candidates"] == 2
    assert metrics["deduplicated_candidates"] == 1
    assert duplicate.state == "DEDUPLICATED"
    assert len(app.candidates) == 1


def test_stale_position_does_not_close_before_hard_threshold() -> None:
    repository = MemoryRepository()
    record = _closing_record().model_copy(update={
        "state": DemoExecutionState.DEMO_POSITION_OPEN,
        "close_order_id": None,
    })
    repository.records[str(record.candidate_id)] = record
    client = FakeDemoClient()
    service = DemoExecutionService(
        demo_settings(v2_position_data_stale_exit_seconds=120),
        repository,
        client,
        run_id=record.run_id,
    )
    result = service.monitor_strategy_position(
        str(record.id), Decimal("65000"), data_fresh=False,
        stale_feature="public trades are stale", stale_age_seconds=20,
        stale_exit_threshold_seconds=120,
        now=record.created_at + timedelta(seconds=30),
    )
    assert result is not None
    assert result.state == DemoExecutionState.DEMO_POSITION_OPEN
    assert result.position_data_stale_feature == "public trades are stale"
    assert result.position_data_stale_threshold_seconds == 120
    assert result.position_data_stale_protection_confirmed is True
    assert client.orders == []
