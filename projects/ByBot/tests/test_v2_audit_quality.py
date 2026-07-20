from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.bybit.demo import (
    DemoExecutionService,
    attribute_exchange_close,
    canonical_exit_attribution,
)
from app.config import NewsClassifierMode, Settings
from app.models import DemoExecutionRecord, DemoExecutionState, NewsItem, Side, Symbol
from app.news.classifier import CodexCLINewsClassifier, build_news_classifier
from app.v2.analytics import (
    V2ReportGenerator, _liquidation_metrics_at, _news_funnel_blockers, _trade_row,
)
from app.v2.market import RollingFeatureEngine
from app.v2.models import SourceHealth, StrategyName
from app.v2.news import EntityMapper, V2NewsAggregator
from app.v2.runtime import V2Runtime
from tests.test_v2_runtime_observability import (
    Aggregator,
    Execution,
    Features,
    MemoryRepository,
    Portfolio,
    Universe,
    runtime,
    settings as runtime_settings,
)
from tests.test_v2_system import feature


def _record(side: Side = Side.SELL) -> DemoExecutionRecord:
    return DemoExecutionRecord(
        candidate_id=uuid4(), run_id="audit", order_link_id="entry-link",
        state=DemoExecutionState.DEMO_POSITION_OPEN, symbol=Symbol.WIFUSDT,
        side=side, requested_quantity=Decimal("163"),
        accepted_quantity=Decimal("163"), average_fill_price=Decimal("0.15311"),
        stop_loss=Decimal("0.15348"), take_profit=Decimal("0.15256"),
        created_at=datetime.fromtimestamp(1784152882, tz=timezone.utc),
    )


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_exchange_generated_stop_loss_is_metadata_attributed(side: Side) -> None:
    record = _record(side)
    result = attribute_exchange_close(record, {
        "orderId": "sl-order", "orderLinkId": "", "stopOrderType": "StopLoss",
        "createType": "CreateByStopLoss", "reduceOnly": True,
        "closeOnTrigger": True, "triggerPrice": str(record.stop_loss),
    }, source="test")
    assert result == "stop_loss"
    assert record.close_reason == "stop_loss"
    assert record.exit_attribution_evidence["order_link_id"] is None


def test_exchange_generated_take_profit_is_metadata_attributed() -> None:
    record = _record()
    assert attribute_exchange_close(record, {
        "orderId": "tp-order", "stopOrderType": "TakeProfit",
        "createType": "CreateByTakeProfit", "reduceOnly": True,
        "closeOnTrigger": True,
    }, source="test") == "take_profit"


class _ExecutionRepo:
    def __init__(self, record: DemoExecutionRecord) -> None:
        self.record = record
        self.events: list[str] = []

    def load_demo_kill_switch(self):
        return None

    def load_demo_executions(self):
        return [self.record]

    def save_demo_execution(self, record, *, event_type):
        self.record = record
        self.events.append(event_type)
        return True


def _service(record: DemoExecutionRecord) -> tuple[DemoExecutionService, _ExecutionRepo]:
    repository = _ExecutionRepo(record)
    return DemoExecutionService(Settings(_env_file=None), repository, None), repository


def _close_fill() -> dict[str, object]:
    return {
        "execId": "close-exec", "orderId": "close-order", "execQty": "163",
        "execPrice": "0.15346", "execFee": "0.013", "execTime": "1784152925050",
        "closedSize": "163", "side": "Buy", "stopOrderType": "StopLoss",
        "createType": "CreateByStopLoss", "orderLinkId": "",
    }


def test_position_update_before_close_fill_preserves_exact_attribution() -> None:
    record = _record(); record.state = DemoExecutionState.DEMO_CLOSING
    service, repository = _service(record)
    service._apply_position_update(record, {"symbol": "WIFUSDT", "size": "0"})
    service._apply_fill(record, _close_fill(), force_close=True)
    assert repository.record.exit_attribution == "stop_loss"
    assert "POSITION_FLAT_PENDING_PNL" in repository.events


def test_close_fill_before_position_update_preserves_exact_attribution() -> None:
    record = _record(); service, repository = _service(record)
    service._apply_fill(record, _close_fill(), force_close=True)
    service._apply_position_update(record, {"symbol": "WIFUSDT", "size": "0"})
    assert repository.record.exit_attribution == "stop_loss"
    assert len(repository.record.close_fills) == 1


@pytest.mark.parametrize(
    "reason,expected",
    [("invalidated_setup", "strategy_exit"), ("stale_signal", "stale_signal"),
     ("runner_cleanup", "forced_cleanup")],
)
def test_bot_close_reasons_use_canonical_taxonomy(reason: str, expected: str) -> None:
    assert canonical_exit_attribution(reason) == expected


def test_manual_reduce_only_close_is_external() -> None:
    record = _record()
    assert attribute_exchange_close(record, {
        "orderId": "manual", "reduceOnly": True, "createType": "CreateByUser"
    }, source="test") == "manual_external_close"


@pytest.mark.parametrize(
    "metadata,expected_state,expected_attribution",
    [
        (
            {"stopOrderType": "TakeProfit", "createType": "CreateByTakeProfit"},
            DemoExecutionState.DEMO_CLOSED,
            "take_profit",
        ),
        (
            {"stopOrderType": "StopLoss", "createType": "CreateByStopLoss"},
            DemoExecutionState.DEMO_CLOSED,
            "stop_loss",
        ),
        (
            {"createType": "CreateByClosing"},
            DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
            "manual_external_close",
        ),
    ],
)
def test_exact_exchange_close_terminalizes_directly_from_position_open(
    metadata: dict[str, str],
    expected_state: DemoExecutionState,
    expected_attribution: str,
) -> None:
    record = _record()
    service, repository = _service(record)
    order = {
        "symbol": "WIFUSDT", "orderId": "close-order", "side": "Buy",
        "orderStatus": "Filled", "reduceOnly": True, "closeOnTrigger": True,
        "qty": "163", "cumExecQty": "163",
        "createdTime": "1784152925050", "updatedTime": "1784152925050",
        **metadata,
    }
    fill = _close_fill()
    fill.pop("stopOrderType", None)
    fill.pop("createType", None)
    fill.update(metadata)

    assert service._finalize_attributed_flat_close(
        record, realtime=[], history=[order], executions=[fill],
        positions=[{"symbol": "WIFUSDT", "size": "0"}],
    )
    assert repository.record.state == expected_state
    assert repository.record.exit_attribution == expected_attribution
    assert repository.record.close_order_id == "close-order"


def test_trade_export_never_has_blank_null_or_unknown_exit() -> None:
    row = _trade_row(_record().model_dump(mode="json"))
    assert row["exit_attribution"] == "unattributed_external_close"
    assert row["exit_reason"] == "unattributed_external_close"


def test_summary_exports_grouped_latency_and_flags_unattributed_exit(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    execution = _record().model_dump(mode="json")
    execution.update({
        "id": str(uuid4()), "run_id": "report-run", "state": "DEMO_CLOSED",
        "strategy_name": "VolumeBreakoutStrategy", "signal_created_at": now.isoformat(),
        "order_submitted_at": (now + timedelta(milliseconds=20)).isoformat(),
        "order_acknowledged_at": (now + timedelta(milliseconds=30)).isoformat(),
        "first_fill_at": (now + timedelta(milliseconds=50)).isoformat(),
    })

    class Repo:
        def v2_report_rows(self, run_id):
            return {
                "signals": [], "rejections": [], "incidents": [],
                "executions": [execution], "runtime": {},
            }

    report = V2ReportGenerator(Repo(), str(tmp_path)).generate("report-run")
    assert report["latency_ms"]["total_run"]["signal_to_order"]["p50"] == 20
    assert report["latency_ms"]["by_strategy"]["VolumeBreakoutStrategy"]
    assert report["latency_ms"]["by_symbol"]["WIFUSDT"]
    assert report["analytics_result"] == "FAIL"
    assert report["unattributed_exit_count"] == 1


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Bitcoin hits $65.5K as more surprise US inflation data sparks three-week BTC price high", Symbol.BTCUSDT),
        ("Is Robinhood Chain’s success bullish or bearish for ETH the asset?", Symbol.ETHUSDT),
        ("Aave launches V4 on Avalanche, laying groundwork for tokenized credit markets", Symbol.AVAXUSDT),
    ],
)
def test_observed_entity_aliases_map_exactly(title: str, expected: Symbol) -> None:
    assert expected in EntityMapper().symbols_for_text(title)


@pytest.mark.parametrize(
    "text,forbidden",
    [
        ("a solution for linking systems", {Symbol.SOLUSDT, Symbol.LINKUSDT}),
        ("price is near support", {Symbol.NEARUSDT}),
        ("the result suits investors", {Symbol.SUIUSDT}),
    ],
)
def test_short_aliases_do_not_match_substrings(text: str, forbidden: set[Symbol]) -> None:
    assert forbidden.isdisjoint(EntityMapper().symbols_for_text(text))


class _Source:
    reliability = 1.0

    def __init__(self, name: str, item: NewsItem | None = None, *, fail: bool = False) -> None:
        self.name = name; self.item = item; self.fail = fail; self.calls = 0

    async def fetch(self):
        self.calls += 1
        if self.fail:
            raise TimeoutError("isolated")
        return [self.item] if self.item else []


def _item(url: str = "https://example.test/story") -> NewsItem:
    return NewsItem(
        title="Bitcoin ETF approval", summary="BTC institutional adoption",
        source="source-a", url=url, published_at=datetime.now(timezone.utc),
    )


def test_news_source_cadence_and_raw_unique_metrics() -> None:
    source = _Source("source-a", _item())
    service = V2NewsAggregator([source], poll_interval_seconds=180)
    now = datetime.now(timezone.utc)
    assert len(asyncio.run(service.poll(now=now))) == 1
    assert asyncio.run(service.poll(now=now + timedelta(seconds=179))) == []
    assert source.calls == 1
    asyncio.run(service.poll(now=now + timedelta(seconds=180)))
    assert source.calls == 2
    metrics = service.source_metrics["source-a"]
    assert metrics["raw_feed_items_received"] == 2
    assert metrics["unique_items_discovered"] == 1
    assert metrics["duplicate_items_seen"] == 1


def test_news_sources_have_separate_cadence_and_failure_isolation() -> None:
    first = _Source("first", _item("https://example.test/one"))
    second = _Source("second", _item("https://example.test/two"), fail=True)
    service = V2NewsAggregator([first, second], retries=0, poll_interval_seconds=180)
    now = datetime.now(timezone.utc)
    rows = asyncio.run(service.poll(now=now))
    service._next_fetch_at[id(first)] = now + timedelta(hours=1)
    service._next_fetch_at[id(second)] = now
    asyncio.run(service.poll(now=now + timedelta(seconds=1)))
    assert len(rows) == 1 and first.calls == 1 and second.calls == 2


def test_durable_duplicate_state_restores_across_restart() -> None:
    item = _item()
    source = _Source("source-a", item)
    restarted = V2NewsAggregator([source], poll_interval_seconds=180)
    restarted.restore_deduplication([item])
    assert asyncio.run(restarted.poll(now=datetime.now(timezone.utc))) == []
    assert restarted.source_metrics["source-a"]["duplicate_items_not_reinserted"] == 1


def test_news_models_come_from_settings_and_failure_exports_primary(monkeypatch) -> None:
    config = Settings(
        _env_file=None, news_classifier_mode=NewsClassifierMode.CODEX_CLI,
        codex_cli_enabled=True, news_primary_model="primary-test",
        news_fallback_model="fallback-test",
    )
    monkeypatch.setattr("app.news.classifier._resolve_executable", lambda _: "codex")
    classifier = build_news_classifier(config)
    assert classifier.provider.model == "primary-test"
    assert classifier.provider.fallback_model == "fallback-test"
    failed = CodexCLINewsClassifier(config, None).classify(_item())
    assert failed.model_name == "primary-test"
    assert failed.fallback_used is False
    assert "gpt-4.1-mini" not in Path("app/news/classifier.py").read_text(encoding="utf-8")


def test_stale_feature_and_critical_incident_metrics_are_separate(tmp_path: Path) -> None:
    app, _, _ = runtime(tmp_path, (Symbol.BTCUSDT,))
    candidate = app.strategies[1].evaluate(feature(fresh=False))
    candidate.feature_snapshot.stale_evidence = [{"source": "trades"}]
    admitted = app._admit(candidate)
    assert not admitted.admitted
    assert app.stale_metrics["stale_feature_rejections"] == 1
    assert app.stale_metrics["stale_rejections_by_source"]["trades"] == 1
    assert app.stale_metrics["stale_rejections_by_symbol"]["BTCUSDT"] == 1
    assert app.stale_metrics["stale_rejections_by_strategy"]["VolumeBreakoutStrategy"] == 1
    assert app.features.stale_incidents == 0
    engine = RollingFeatureEngine(runtime_settings(tmp_path))
    engine.record_critical_stale_incident()
    assert engine.stale_incidents == 1


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"liquidation_feed_initialized": False}, "liquidation_feed_never_initialized"),
        ({"liquidation_feed_initialized": True, "liquidation_feed_available": False}, "liquidation_feed_unavailable"),
        ({"liquidation_feed_initialized": True, "liquidation_data_valid": False}, "liquidation_data_invalid"),
        ({"liquidation_feed_initialized": True, "liquidation_data_age_seconds": 120.0}, None),
    ],
)
def test_liquidation_not_applicable_reasons(tmp_path: Path, updates, reason) -> None:
    app, _, _ = runtime(tmp_path, (Symbol.BTCUSDT,))
    snapshot = feature().model_copy(update=updates)
    assert app._liquidation_not_applicable_reason(snapshot) == reason
    healthy = snapshot.model_copy(update={
        "liquidation_feed_initialized": True, "liquidation_feed_available": True,
        "liquidation_data_valid": True, "liquidation_data_age_seconds": 1.0,
        "liquidation_last_valid_at": datetime.now(timezone.utc),
    })
    assert app._liquidation_not_applicable_reason(healthy) is None


class _RecordingExecution(Execution):
    def __init__(self, repository: MemoryRepository) -> None:
        super().__init__(); self.repository = repository; self.called_at: list[float] = []

    def execute(self, candidate):
        self.called_at.append(time.perf_counter())
        assert candidate.candidate_persisted_at is not None
        assert candidate.execution_queue_entered_at is not None
        assert any(item.id == candidate.id for item in self.repository.signals)
        self.calls += 1
        return {"execution_attempted": False}


class _SlowFeatures(Features):
    def __init__(self) -> None:
        super().__init__(); self.slow_finished_at: float | None = None

    def snapshot(self, symbol, btc_snapshot=None):
        if symbol == Symbol.ETHUSDT:
            time.sleep(0.25)
            self.slow_finished_at = time.perf_counter()
        return feature(symbol)


def test_admitted_candidate_dispatches_before_unrelated_slow_symbol_finishes(tmp_path: Path) -> None:
    app, repository, _ = runtime(tmp_path, (Symbol.BTCUSDT, Symbol.ETHUSDT))
    app.settings.v2_auto_demo_execution = True
    app.features = _SlowFeatures()
    execution = _RecordingExecution(repository); app.execution = execution
    started = time.perf_counter()
    asyncio.run(app._process_market_strategies("latency-cycle"))
    assert execution.called_at
    assert execution.called_at[0] - started < 0.15
    assert app.features.slow_finished_at is not None
    assert execution.called_at[0] < app.features.slow_finished_at


class _SlowNewsAggregator(Aggregator):
    def __init__(self) -> None:
        super().__init__(); self.finished_at: float | None = None

    async def poll(self):
        await asyncio.sleep(0.25)
        self.finished_at = time.perf_counter()
        return []


def test_slow_news_poll_does_not_block_market_execution_dispatch(tmp_path: Path) -> None:
    aggregator = _SlowNewsAggregator()
    app, repository, _ = runtime(
        tmp_path, (Symbol.BTCUSDT,), aggregator=aggregator
    )
    app.settings.v2_auto_demo_execution = True
    execution = _RecordingExecution(repository); app.execution = execution
    asyncio.run(app.cycle())
    assert execution.called_at and aggregator.finished_at
    assert execution.called_at[0] < aggregator.finished_at


class _RowsSource(_Source):
    def __init__(self, name: str, rows: list[object]) -> None:
        super().__init__(name)
        self.rows = rows

    async def fetch(self):
        self.calls += 1
        return list(self.rows)


def _assert_news_funnel(metrics: dict[str, int]) -> None:
    assert metrics["raw_feed_items_received"] == (
        metrics["invalid_feed_items"]
        + metrics["duplicate_within_poll"]
        + metrics["duplicate_within_run"]
        + metrics["duplicate_from_previous_run"]
        + metrics["unique_items_discovered"]
    )
    assert metrics["unique_items_discovered"] == (
        metrics["deterministic_filter_accepts"]
        + metrics["deterministic_filter_rejections"]
    )
    assert _news_funnel_blockers({"source": metrics}) == []


def test_news_funnel_distinguishes_previous_poll_run_and_unique_items() -> None:
    now = datetime.now(timezone.utc)
    previous = _item("https://example.test/previous")
    duplicate_a = _item("https://example.test/within-poll").model_copy(update={
        "title": "Ethereum ETF approval", "summary": "ETH institutional adoption",
    })
    duplicate_b = duplicate_a.model_copy(update={"id": uuid4()})
    unrelated = NewsItem(
        title="Local weather update", summary="Nothing about digital assets",
        source="source", url="https://example.test/unrelated", published_at=now,
    )
    accepted = _item("https://example.test/accepted").model_copy(update={
        "title": "Avalanche institutional adoption",
        "summary": "AVAX institutional demand",
    })
    source = _RowsSource(
        "source", [previous, duplicate_a, duplicate_b, unrelated, accepted]
    )
    aggregator = V2NewsAggregator([source], poll_interval_seconds=10, run_id="run")
    aggregator.restore_deduplication([previous])
    rows = asyncio.run(aggregator.poll(now=now))
    assert [row[0].url for row in rows] == [duplicate_a.url, accepted.url]
    asyncio.run(aggregator.poll(now=now + timedelta(seconds=10)))
    metrics = aggregator.source_metrics["source"]
    assert metrics["duplicate_from_previous_run"] >= 1
    assert metrics["duplicate_within_poll"] >= 1
    assert metrics["duplicate_within_run"] >= 1
    assert metrics["deterministic_filter_rejections"] >= 1
    _assert_news_funnel(metrics)


def test_news_funnel_counts_invalid_feed_rows() -> None:
    source = _RowsSource("source", [{"id": "only-id"}])
    aggregator = V2NewsAggregator([source], retries=0)
    assert asyncio.run(aggregator.poll()) == []
    metrics = aggregator.source_metrics["source"]
    assert metrics["invalid_feed_items"] == 1
    _assert_news_funnel(metrics)


def test_latency_uses_local_receive_clock_and_never_exports_negative() -> None:
    now = datetime.now(timezone.utc)
    record = _record().model_dump(mode="json")
    record.update({
        "local_submit_started_at": now.isoformat(),
        "local_ack_received_at": (now + timedelta(milliseconds=50)).isoformat(),
        "local_fill_received_at": (now + timedelta(milliseconds=80)).isoformat(),
        "first_fill_at": (now - timedelta(seconds=2)).isoformat(),
        "signal_created_at": (now - timedelta(milliseconds=10)).isoformat(),
    })
    row = _trade_row(record)
    assert row["ack_to_first_fill_ms"] == pytest.approx(30)
    assert row["order_submit_to_first_fill_ms"] == pytest.approx(80)
    assert row["latency_validation_errors"] == []


def test_fill_received_before_ack_is_explicit_and_latency_is_null() -> None:
    now = datetime.now(timezone.utc)
    record = _record().model_dump(mode="json")
    record.update({
        "local_submit_started_at": now.isoformat(),
        "local_fill_received_at": (now + timedelta(milliseconds=20)).isoformat(),
        "local_ack_received_at": (now + timedelta(milliseconds=30)).isoformat(),
    })
    row = _trade_row(record)
    assert row["fill_before_ack"] is True
    assert row["ack_to_first_fill_ms"] is None
    assert all(
        value is None or not isinstance(value, (int, float)) or value >= 0
        for key, value in row.items() if key.endswith("_ms")
    )


def test_exchange_fill_timestamp_is_never_subtracted_from_local_ack() -> None:
    now = datetime.now(timezone.utc)
    record = _record().model_dump(mode="json")
    record.update({
        "order_acknowledged_at": now.isoformat(),
        "first_fill_at": (now - timedelta(milliseconds=74)).isoformat(),
    })
    row = _trade_row(record)
    assert row["ack_to_first_fill_ms"] is None
    assert row["latency_validation_errors"] == []
    assert "local_fill_receipt_unavailable" in row["latency_diagnostic_codes"]


def test_news_model_usage_rejects_strategy_contamination(tmp_path: Path) -> None:
    app, _, _ = runtime(tmp_path, (Symbol.BTCUSDT,))
    common = {
        "news_id": uuid4(), "fallback_used": False, "fallback_reason": None,
        "classified_at": datetime.now(timezone.utc),
    }
    app._record_news_model_usage(SimpleNamespace(
        **common, provider_name="deterministic-v2",
        model_name="VolumeBreakoutStrategy",
    ))
    assert app.status()["last_news_model_used"] is None
    app._record_news_model_usage(SimpleNamespace(
        **common, provider_name="codex_cli",
        model_name=app.settings.news_primary_model,
    ))
    assert app.status()["last_news_model_used"] == "gpt-5.4-mini"
    app._record_news_model_usage(SimpleNamespace(
        **{**common, "fallback_used": True, "fallback_reason": "ambiguous"},
        provider_name="codex_cli", model_name=app.settings.news_fallback_model,
    ))
    status = app.status()
    assert status["last_news_model_used"] == "gpt-5.6-luna"
    assert status["last_news_fallback_used"] is True


def test_liquidation_ages_are_recalculated_from_one_generated_at() -> None:
    generated = datetime.now(timezone.utc)
    stamp = generated - timedelta(seconds=1200)
    metrics, blockers = _liquidation_metrics_at({
        "liquidation_eligibility_by_symbol": {
            "BTCUSDT": {
                "symbol": "BTCUSDT", "last_valid_timestamp": stamp.isoformat(),
                "current_age_seconds": 58.0, "state": "INELIGIBLE",
                "not_applicable_reason": "liquidation_feed_stale",
            }
        }
    }, generated)
    assert metrics["most_recent_age_seconds"] == pytest.approx(1200)
    assert metrics["maximum_age_seconds"] == pytest.approx(1200)
    assert blockers == []


def test_stale_open_position_is_warning_not_critical_incident(tmp_path: Path) -> None:
    app, repository, _ = runtime(tmp_path, (Symbol.BTCUSDT,))
    record = _record(Side.BUY).model_copy(update={
        "run_id": app.run_id, "symbol": Symbol.BTCUSDT,
        "state": DemoExecutionState.DEMO_POSITION_OPEN,
    })
    repository.load_demo_executions = lambda: [record]
    stale = feature(Symbol.BTCUSDT, fresh=False).model_copy(update={
        "stale_evidence": [{
            "source": "trades", "observed_age_seconds": 61,
            "configured_maximum_age_seconds": 30,
        }]
    })
    app.features.snapshot = lambda *args, **kwargs: stale
    app._monitor_positions()
    assert app.features.stale_incidents == 0
    assert app.stale_metrics["position_stale_observations"] == 1
    incident = next(iter(repository.incidents.values()))
    assert incident.event_type == "V2_STALE_POSITION_OBSERVATION"
    assert incident.payload["classified_as"] == "stale_feature_warning"


def test_analytics_result_fails_for_run_consistency_errors(tmp_path: Path) -> None:
    generated = datetime.now(timezone.utc)

    class Repo:
        def v2_report_rows(self, run_id):
            return {
                "signals": [], "rejections": [], "executions": [], "incidents": [],
                "runtime": {
                    "news_primary_model": "gpt-5.4-mini",
                    "news_fallback_model": "gpt-5.6-luna",
                    "last_news_model_used": "VolumeBreakoutStrategy",
                    "news_source_metrics": {
                        "rss": {
                            "raw_feed_items_received": 10,
                            "invalid_feed_items": 0,
                            "duplicate_within_poll": 0,
                            "duplicate_within_run": 0,
                            "duplicate_from_previous_run": 0,
                            "unique_items_discovered": 0,
                            "deterministic_filter_accepts": 0,
                            "deterministic_filter_rejections": 10,
                        }
                    },
                    "liquidation_metrics": {
                        "liquidation_eligibility_by_symbol": {
                            "BTCUSDT": {
                                "last_valid_timestamp": None,
                                "current_age_seconds": 58,
                                "state": "ELIGIBLE",
                                "not_applicable_reason": None,
                            }
                        }
                    },
                },
            }

    report = V2ReportGenerator(Repo(), str(tmp_path)).generate("invalid-run")
    assert report["analytics_result"] == "FAIL"
    assert "last_news_model_used is not a configured news model" in report["analytics_blockers"]
    assert "news funnel raw invariant failed: rss" in report["analytics_blockers"]
    assert "liquidation null timestamp has non-null age: BTCUSDT" in report["analytics_blockers"]
