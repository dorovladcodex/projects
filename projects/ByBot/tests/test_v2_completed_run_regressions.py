from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.bybit.demo import DemoExchangeError, DemoExecutionService
from app.config import Settings
from app.models import DemoExecutionRecord, DemoExecutionState, DemoFill, Side, Symbol
from app.v2.analytics import _trade_row
from app.v2.market import BybitPublicWebSocketEngine, RollingFeatureEngine
from app.v2.models import PortfolioReservation, ReservationState, StrategyName
from app.v2.runtime import V2Runtime, _news_classification_reason, _news_skip_reason
from tests.test_bybit_demo_execution import FakeDemoClient, MemoryRepository, demo_settings
from tests.test_v2_runtime_observability import runtime
from tests.test_v2_system import feature


def _closing_record(
    quantity: str = "0.010", *, symbol: Symbol = Symbol.BTCUSDT,
    suffix: str = "",
) -> DemoExecutionRecord:
    entry_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    candidate_id = uuid4()
    return DemoExecutionRecord(
        candidate_id=candidate_id,
        run_id="review-run",
        order_link_id=f"bybot-review-entry{suffix}",
        order_id=f"entry-order{suffix}",
        close_order_id=f"close-order{suffix}",
        state=DemoExecutionState.DEMO_CLOSING,
        symbol=symbol,
        side=Side.BUY,
        requested_quantity=Decimal(quantity),
        accepted_quantity=Decimal(quantity),
        average_fill_price=Decimal("65000"),
        stop_loss=Decimal("64000"),
        take_profit=Decimal("67000"),
        protection_confirmed=True,
        fills=[DemoFill(
            execution_id=f"entry-exec{suffix}",
            order_id=f"entry-order{suffix}",
            quantity=Decimal(quantity),
            price=Decimal("65000"),
            fee=Decimal("0.10"),
            executed_at=entry_at,
        )],
        created_at=entry_at - timedelta(seconds=1),
    )


def _close_payload(
    record: DemoExecutionRecord, quantity: str = "0.010", *,
    kind: str = "StopLoss", price: str = "64900",
) -> tuple[dict, dict]:
    close_ms = int((record.fills[0].executed_at + timedelta(minutes=1)).timestamp() * 1000)
    order = {
        "symbol": record.symbol.value,
        "orderId": record.close_order_id,
        "orderStatus": "Filled",
        "orderLinkId": "",
        "side": "Sell",
        "reduceOnly": True,
        "closeOnTrigger": True,
        "stopOrderType": kind,
        "createType": f"CreateBy{kind}",
        "cumExecQty": quantity,
        "qty": quantity,
        "avgPrice": price,
        "updatedTime": str(close_ms),
    }
    fill = {
        "symbol": record.symbol.value,
        "orderId": record.close_order_id,
        "orderLinkId": "",
        "execId": f"{record.close_order_id}-exec",
        "execQty": quantity,
        "execPrice": price,
        "execFee": "0.10",
        "feeCurrency": "USDT",
        "execTime": str(close_ms),
        "side": "Sell",
        "stopOrderType": kind,
        "createType": f"CreateBy{kind}",
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
    assert repository.saved_events.count(("DEMO_CLOSE_TERMINALIZED", "DEMO_CLOSED")) == 1
    service._reconcile_execution_rest(result)
    assert repository.saved_events.count(("DEMO_CLOSE_TERMINALIZED", "DEMO_CLOSED")) == 1


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
    assert "DEMO_CLOSE_TERMINALIZED" not in [event for event, _ in repository.saved_events]


@pytest.mark.parametrize(
    "kind,price,attribution",
    [
        ("StopLoss", "64900", "stop_loss"),
        ("TakeProfit", "65100", "take_profit"),
    ],
)
def test_ws_full_close_immediately_terminalizes_with_exact_rest_evidence(
    kind: str, price: str, attribution: str,
) -> None:
    repository = MemoryRepository()
    record = _closing_record()
    repository.records[str(record.candidate_id)] = record
    client = FakeDemoClient()
    order, fill = _close_payload(record, kind=kind, price=price)
    client.history = [order]
    client.executions = [fill]
    service = DemoExecutionService(
        demo_settings(), repository, client, run_id=record.run_id
    )

    service.handle_private_event({"topic": "execution", "data": [fill]})

    saved = repository.get_demo_execution(str(record.candidate_id))
    assert saved.state == DemoExecutionState.DEMO_CLOSED
    assert saved.exit_attribution == attribution
    assert saved.close_order_id == record.close_order_id
    assert sum(item.quantity for item in saved.close_fills) == Decimal("0.010")
    assert saved.gross_realized_pnl == (
        (Decimal(price) - Decimal("65000")) * Decimal("0.010")
    )
    assert saved.exchange_fees == Decimal("0.20")
    assert saved.realized_exchange_pnl == saved.gross_realized_pnl - Decimal("0.20")
    assert client.orders == []
    assert client.cancelled == []


def test_duplicate_ws_and_rest_race_persists_one_terminal_transition() -> None:
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
    event = {"topic": "execution", "data": [fill]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda action: action(), [
            lambda: service.handle_private_event(event),
            lambda: service._reconcile_execution_rest(record),
        ]))
    service.handle_private_event(event)

    assert repository.saved_events.count(
        ("DEMO_CLOSE_TERMINALIZED", "DEMO_CLOSED")
    ) == 1
    saved = repository.get_demo_execution(str(record.candidate_id))
    assert len(saved.close_fills) == 1


def test_actual_sol_btc_two_close_fixture_terminalizes_exactly_once() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "two_close_terminalization_20260729.json"
        ).read_text(encoding="utf-8")
    )
    repository = MemoryRepository()
    client = FakeDemoClient()
    records: list[DemoExecutionRecord] = []
    for item in fixture["executions"]:
        entry_at = datetime.fromisoformat(item["entry_time"])
        close_at = datetime.fromisoformat(item["close_time"])
        quantity = Decimal(item["quantity"])
        record = DemoExecutionRecord(
            id=item["execution_id"],
            candidate_id=item["candidate_id"],
            run_id=fixture["run_id"],
            order_link_id=item["entry_order_link_id"],
            order_id=item["entry_order_id"],
            close_order_link_id=item["close_order_link_id"],
            close_order_id=item["close_order_id"],
            state=DemoExecutionState(item["state"]),
            symbol=Symbol(item["symbol"]),
            side=Side.BUY,
            requested_quantity=quantity,
            accepted_quantity=quantity,
            average_fill_price=Decimal(item["entry_price"]),
            tp_order_id=item["tp_order_id"],
            sl_order_id=item["sl_order_id"],
            protection_confirmed=True,
            close_reason="unattributed_external_close",
            exit_attribution="unattributed_external_close",
            fills=[DemoFill(
                execution_id=item["entry_exec_id"],
                order_id=item["entry_order_id"],
                quantity=quantity,
                price=Decimal(item["entry_price"]),
                fee=Decimal(item["entry_fee"]),
                fee_currency="USDT",
                executed_at=entry_at,
            )],
            created_at=entry_at - timedelta(seconds=1),
        )
        repository.records[str(record.candidate_id)] = record
        records.append(record)
        close_ms = int(close_at.timestamp() * 1000)
        client.history.append({
            "symbol": item["symbol"],
            "orderId": item["close_order_id"],
            "orderLinkId": item["close_order_link_id"],
            "orderStatus": "Filled",
            "side": "Sell",
            "reduceOnly": True,
            "closeOnTrigger": False,
            "createType": "CreateByUser",
            "stopOrderType": "",
            "cumExecQty": item["quantity"],
            "qty": item["quantity"],
            "avgPrice": item["close_price"],
            "updatedTime": str(close_ms),
        })
        client.executions.append({
            "symbol": item["symbol"],
            "orderId": item["close_order_id"],
            "orderLinkId": item["close_order_link_id"],
            "execId": item["close_exec_id"],
            "execQty": item["quantity"],
            "execPrice": item["close_price"],
            "execFee": item["close_fee"],
            "feeCurrency": "USDT",
            "feeRate": "0.00055",
            "execTime": str(close_ms),
            "side": "Sell",
            "closedSize": item["quantity"],
            "createType": "CreateByUser",
        })
    service = DemoExecutionService(
        demo_settings(), repository, client, run_id=fixture["run_id"]
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        reconciled = list(pool.map(service._reconcile_execution_rest, records))

    assert all(
        item.state == DemoExecutionState.DEMO_CLOSED_EXTERNALLY
        for item in reconciled
    )
    assert [item.realized_exchange_pnl for item in reconciled] == [
        Decimal(item["expected_net_pnl"]) for item in fixture["executions"]
    ]
    assert all(item.terminalization_started_at for item in reconciled)
    assert all(item.evidence_acquired_at for item in reconciled)
    assert all(item.terminalization_completed_at for item in reconciled)
    assert all(item.terminalization_attempt_count >= 1 for item in reconciled)
    assert all(item.execution_lock_wait_ms is not None for item in reconciled)
    assert all(item.persistence_commit_ms is not None for item in reconciled)
    assert repository.saved_events.count(
        ("DEMO_CLOSE_TERMINALIZED", "DEMO_CLOSED_EXTERNALLY")
    ) == 2
    for item in reconciled:
        service._reconcile_execution_rest(item)
    assert repository.saved_events.count(
        ("DEMO_CLOSE_TERMINALIZED", "DEMO_CLOSED_EXTERNALLY")
    ) == 2


def test_remote_nonflat_and_conflicting_close_remain_unresolved() -> None:
    repository = MemoryRepository()
    record = _closing_record()
    other = _closing_record(symbol=Symbol.ETHUSDT, suffix="-other")
    other.close_order_id = record.close_order_id
    repository.records[str(record.candidate_id)] = record
    repository.records[str(other.candidate_id)] = other
    client = FakeDemoClient()
    order, fill = _close_payload(record)
    client.history = [order]
    client.executions = [fill]
    client.positions[0]["size"] = "0.010"
    service = DemoExecutionService(
        demo_settings(), repository, client, run_id=record.run_id
    )

    assert not service._finalize_attributed_flat_close(
        record, realtime=[], history=[order], executions=[fill],
        positions=client.positions,
    )
    client.positions[0]["size"] = "0"
    assert not service._finalize_attributed_flat_close(
        record, realtime=[], history=[order], executions=[fill],
        positions=client.positions,
    )
    assert record.state == DemoExecutionState.DEMO_CLOSING


def test_three_simultaneous_stop_losses_terminalize_independently() -> None:
    repository = MemoryRepository()
    records = [
        _closing_record(symbol=symbol, suffix=f"-{index}")
        for index, symbol in enumerate(
            (Symbol.LINKUSDT, Symbol.WIFUSDT, Symbol.ETHUSDT), start=1
        )
    ]
    client = FakeDemoClient()
    for record in records:
        repository.records[str(record.candidate_id)] = record
        order, fill = _close_payload(record)
        client.history.append(order)
        client.executions.append(fill)
    service = DemoExecutionService(
        demo_settings(), repository, client, run_id="review-run"
    )

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(service._reconcile_execution_rest, records))

    assert all(item.state == DemoExecutionState.DEMO_CLOSED for item in results)
    assert repository.saved_events.count(
        ("DEMO_CLOSE_TERMINALIZED", "DEMO_CLOSED")
    ) == 3
    assert client.orders == []


def test_unexpected_terminal_reconciliation_failure_is_not_swallowed() -> None:
    repository = MemoryRepository()
    record = _closing_record()
    repository.records[str(record.candidate_id)] = record

    class BrokenClient(FakeDemoClient):
        def get_order_history(self, symbol=None, settle_coin=None):
            raise RuntimeError("programming defect")

    service = DemoExecutionService(
        demo_settings(), repository, BrokenClient(), run_id=record.run_id
    )
    with pytest.raises(RuntimeError, match="programming defect"):
        service._reconcile_execution_rest(record)


def test_transient_terminalization_failure_is_visible_and_deduplicated() -> None:
    repository = MemoryRepository()
    record = _closing_record()
    repository.records[str(record.candidate_id)] = record
    _, fill = _close_payload(record)

    class UnavailableClient(FakeDemoClient):
        def get_order_history(self, symbol=None, settle_coin=None):
            raise DemoExchangeError("temporary read failure")

    service = DemoExecutionService(
        demo_settings(), repository, UnavailableClient(), run_id=record.run_id
    )
    event = {"topic": "execution", "data": [fill]}

    service.handle_private_event(event)
    service.handle_private_event(event)

    saved = repository.get_demo_execution(str(record.candidate_id))
    assert saved.state == DemoExecutionState.DEMO_CLOSING
    assert service.terminalization_retry_warnings == 1
    assert "temporary read failure" in service.last_terminalization_warning
    assert [event for event, _ in repository.saved_events].count(
        "CLOSE_TERMINALIZATION_RETRY_REQUIRED"
    ) == 1


def test_terminal_sync_releases_reservation_and_updates_cooldown_once() -> None:
    record = _closing_record()
    record.state = DemoExecutionState.DEMO_CLOSED
    record.closed_at = datetime.now(timezone.utc)
    reservation = PortfolioReservation(
        run_id=record.run_id, candidate_id=record.candidate_id,
        execution_id=record.id, symbol=record.symbol,
        strategy_name=StrategyName.VOLUME_BREAKOUT,
        correlation_group="market_core", notional_usdt=Decimal("50"),
        risk_usdt=Decimal("1"), state=ReservationState.OPEN,
    )

    class Repo:
        def load_demo_executions(self):
            return [record]

    class Portfolio:
        ACTIVE = {
            ReservationState.RESERVED,
            ReservationState.EXECUTING,
            ReservationState.OPEN,
        }

        def __init__(self):
            self.reservations = [reservation]
            self.release_calls = 0
            self.cooldown_updates = 0

        def release(self, reservation_id, *, closed_at=None):
            row = self.reservations[0]
            if row.state not in self.ACTIVE:
                return
            self.release_calls += 1
            self.cooldown_updates += 1
            row.state = ReservationState.RELEASED
            row.released_at = closed_at

    portfolio = Portfolio()
    subject = SimpleNamespace(
        run_id=record.run_id, repository=Repo(), portfolio=portfolio,
        signal_metrics={}, strategy_evaluation_counts={}, candidates=[],
    )

    V2Runtime._sync_reservations(subject)
    V2Runtime._sync_reservations(subject)
    metrics = V2Runtime._signal_metrics_snapshot(subject)

    assert portfolio.release_calls == 1
    assert portfolio.cooldown_updates == 1
    assert not [row for row in portfolio.reservations if row.state in portfolio.ACTIVE]
    assert metrics["completed_trades"] == 1


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


def test_exchange_timestamp_rounding_within_ten_ms_is_diagnostic_only() -> None:
    now = datetime.now(timezone.utc)
    row = _trade_row({
        "id": "execution",
        "run_id": "run",
        "state": "DEMO_CLOSED",
        "exchange_order_created_at": now.isoformat(),
        "exchange_fill_at": (now - timedelta(milliseconds=1)).isoformat(),
    })

    assert row["exchange_order_to_fill_ms"] == 0.0
    assert row["latency_validation_errors"] == []
    assert (
        "exchange_clock_rounding:exchange_order_to_fill"
        in row["latency_diagnostic_codes"]
    )


def test_exchange_timestamp_inversion_beyond_tolerance_remains_failure() -> None:
    now = datetime.now(timezone.utc)
    row = _trade_row({
        "id": "execution",
        "run_id": "run",
        "state": "DEMO_CLOSED",
        "exchange_order_created_at": now.isoformat(),
        "exchange_fill_at": (now - timedelta(milliseconds=11)).isoformat(),
    })

    assert row["exchange_order_to_fill_ms"] is None
    assert (
        "negative_latency:exchange_order_to_fill"
        in row["latency_validation_errors"]
    )


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


def test_symbol_liquidation_age_does_not_use_newer_generic_source_message() -> None:
    engine = RollingFeatureEngine(Settings(_env_file=None))
    symbol = Symbol.BTCUSDT
    now = datetime.now(timezone.utc)
    engine.ingest_ticker(symbol, {"lastPrice": "65000", "volume24h": "1"}, now)
    engine.ingest_orderbook(symbol, [["64999", "1"]], [["65001", "1"]], now)
    engine.ingest_trade(symbol, Decimal("65000"), Decimal("0.1"), "Buy", now)
    engine.mark_transport_connected(True, at=now)
    engine.mark_liquidation_subscribed((symbol,), at=now - timedelta(minutes=5))
    engine.ingest_liquidation(
        symbol, "Buy", Decimal("1"), Decimal("100"),
        now - timedelta(minutes=4),
    )
    # A later valid source message for another symbol updates source health but
    # must not rewrite BTCUSDT's symbol-specific liquidation age.
    engine.ingest_liquidation(
        Symbol.ETHUSDT, "Sell", Decimal("1"), Decimal("100"),
        now - timedelta(seconds=10),
    )
    snapshot = engine.snapshot(symbol, now=now)

    assert snapshot.liquidation_data_age_seconds == pytest.approx(240, abs=0.01)
    assert snapshot.source_age_seconds["liquidations"] == pytest.approx(240, abs=0.01)
    assert engine.data_age_metrics()["liquidations"]["latest_message_age"] == pytest.approx(
        10, abs=0.1
    )


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
