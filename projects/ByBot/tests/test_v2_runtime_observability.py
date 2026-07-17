from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.models import NewsItem, Symbol
from app.news import MockNewsClassifier, NewsService
from app.v2.analytics import V2ReportGenerator
from app.v2.market import RollingFeatureEngine
from app.v2.models import SourceState, StrategyName
from app.v2.news import EntityMapper
from app.v2.runtime import V2Runtime
from app.v2.strategies import build_v2_strategies
from tests.test_v2_system import feature


class MemoryRepository:
    available = True

    def __init__(self) -> None:
        self.signals = []
        self.incidents = {}
        self.runtime = {}

    def load_v2_signal_candidates(self, run_id):
        return []

    def load_v2_run_runtime(self, run_id):
        return self.runtime

    def update_v2_run_runtime(self, run_id, payload):
        self.runtime = payload
        return True

    def begin_v2_run(self, run_id, started_at):
        return True

    def save_v2_market_feature(self, snapshot):
        return True

    def save_v2_signal_candidate(self, candidate):
        self.signals.append(candidate)
        return True

    def save_v2_incident(self, incident):
        self.incidents[str(incident.id)] = incident
        return True

    def load_demo_executions(self):
        return []

    def finish_v2_run(self, run_id, report):
        return True

    def v2_report_rows(self, run_id):
        return {
            "signals": [item.model_dump(mode="json") for item in self.signals],
            "rejections": [
                item.model_dump(mode="json") for item in self.signals
                if item.rejection_reason
            ],
            "incidents": [
                item.model_dump(mode="json") for item in self.incidents.values()
            ],
            "executions": [], "runtime": self.runtime,
        }


class Universe:
    def __init__(self, symbols: tuple[Symbol, ...]) -> None:
        self.accepted_symbols = symbols
        self.statuses = {
            symbol: SimpleNamespace(accepted=True, reasons=[]) for symbol in symbols
        }
        self.last_refresh_at = datetime.now(timezone.utc)

    def get(self, symbol):
        return self.statuses.get(symbol)

    def refresh(self, now=None):
        return self.statuses


class Features:
    stale_incidents = 0
    source_states = {}

    def __init__(self, failures: set[Symbol] | None = None) -> None:
        self.failures = failures or set()
        self.calls = []

    def snapshot(self, symbol, btc_snapshot=None):
        self.calls.append(symbol)
        if symbol in self.failures:
            raise ValueError("required market decimal missing")
        return feature(symbol)


class Aggregator:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.sources = [SimpleNamespace(name="mock-rss", reliability=1.0)]
        self.health = {"mock-rss": SourceState(source="mock-rss")}
        self.source_metrics = {
            "mock-rss": {
                "fetch_attempts": 0, "fetch_successes": 0,
                "fetch_failures": 0, "items_received": 0,
            }
        }
        self.items_received = 0
        self.items_rejected = 0
        self.duplicate_count = 0
        self.last_poll_audit = []

    async def poll(self):
        self.source_metrics["mock-rss"]["fetch_attempts"] += 1
        self.source_metrics["mock-rss"]["fetch_successes"] += 1
        self.items_received += len(self.rows)
        self.source_metrics["mock-rss"]["items_received"] += len(self.rows)
        self.last_poll_audit = [{
            "news_id": str(item.id), "source": item.source, "title": item.title,
            "url": item.url, "published_at": item.published_at.isoformat(),
            "received_at": item.received_at.isoformat(),
            "deduplication_status": "unique",
            "detected_entities": [symbol.value.removesuffix("USDT") for symbol in symbols],
            "mapped_symbols": [symbol.value for symbol in symbols],
            "market_wide": len(symbols) > 1,
            "deterministic_filter_decision": "accepted",
        } for item, symbols, _ in self.rows]
        return self.rows


class Portfolio:
    reservations = []
    ACTIVE = set()
    kill_switch_active = False
    kill_switch_reasons = []

    def block_reasons(self, symbol, notional):
        return []


class Execution:
    def __init__(self) -> None:
        self.calls = 0
        self.demo_execution = SimpleNamespace(
            kill_switch_active=False, kill_switch_reasons=[],
            monitor_strategy_position=lambda *args, **kwargs: None,
        )

    def safety_preflight(self, require_auto_execution=False):
        return []

    def execute(self, candidate):
        self.calls += 1
        raise AssertionError("exchange mutation must not run in tests")


def settings(tmp_path: Path, **updates: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None, "v2_enabled": True,
        "v2_auto_demo_execution": False,
        "v2_report_directory": str(tmp_path),
        "v2_min_turnover_24h_usdt": 0,
        "v2_min_orderbook_depth_usdt": 0,
    }
    values.update(updates)
    return Settings(**values)


def runtime(
    tmp_path: Path, symbols: tuple[Symbol, ...], *, failures=None,
    aggregator=None, news_service=None,
):
    repo = MemoryRepository(); execution = Execution()
    app = V2Runtime(
        settings(tmp_path), repo, Universe(symbols), Features(failures),
        aggregator or Aggregator(),
            news_service or NewsService(
                [], MockNewsClassifier(), max_item_age=timedelta(hours=1)
            ),
        Portfolio(), execution, None, run_id="runtime-test",
    )
    app._last_rest_poll_at = datetime.now(timezone.utc)
    return app, repo, execution


def test_sparse_ticker_delta_preserves_last_price(tmp_path) -> None:
    engine = RollingFeatureEngine(settings(tmp_path))
    now = datetime.now(timezone.utc)
    engine.ingest_ticker(Symbol.ETHUSDT, {"lastPrice": "3500"}, now)
    engine.ingest_trade(Symbol.ETHUSDT, feature().last_price, feature().last_price / 100, "BUY", now)
    engine.ingest_orderbook(Symbol.ETHUSDT, [["3499", "10"]], [["3501", "10"]], now)

    engine.ingest_ticker(Symbol.ETHUSDT, {"fundingRate": "0.0001"}, now)

    assert engine.snapshot(Symbol.ETHUSDT, now=now).last_price == 3500


def test_orderbook_delta_updates_levels_without_crossed_negative_spread(tmp_path) -> None:
    engine = RollingFeatureEngine(settings(tmp_path))
    now = datetime.now(timezone.utc)
    engine.ingest_ticker(Symbol.ETHUSDT, {"lastPrice": "3500"}, now)
    engine.ingest_trade(Symbol.ETHUSDT, feature().last_price, feature().last_price / 100, "BUY", now)
    engine.ingest_orderbook(Symbol.ETHUSDT, [["3499", "10"]], [["3501", "10"]], now)

    engine.ingest_orderbook(
        Symbol.ETHUSDT, [["3498", "1"]], [["3502", "1"]], now,
        snapshot=False,
    )

    snapshot = engine.snapshot(Symbol.ETHUSDT, now=now)
    assert snapshot.bid_price == 3499
    assert snapshot.ask_price == 3501
    assert snapshot.spread_bps >= 0


def test_all_accepted_symbols_are_processed_and_no_exchange_mutation(tmp_path) -> None:
    symbols = (Symbol.BTCUSDT, Symbol.ETHUSDT, Symbol.DOGEUSDT, Symbol.WIFUSDT)
    app, _, execution = runtime(tmp_path, symbols)

    asyncio.run(app.cycle())

    assert set(app.status()["symbols_successful"]) == {item.value for item in symbols}
    assert app.symbol_cycle_metrics["BTCUSDT"]["strategies_evaluated"] == 2
    assert app.symbol_cycle_metrics["ETHUSDT"]["strategies_evaluated"] == 2
    assert app.symbol_cycle_metrics["DOGEUSDT"]["strategies_evaluated"] == 3
    assert app.symbol_cycle_metrics["WIFUSDT"]["strategies_evaluated"] == 3
    assert app.strategy_not_applicable_counts["LiquidationMomentumStrategy"] == 4
    assert execution.calls == 0


def test_one_symbol_failure_does_not_block_later_symbols(tmp_path) -> None:
    symbols = (Symbol.BTCUSDT, Symbol.ETHUSDT, Symbol.SOLUSDT)
    app, repo, _ = runtime(tmp_path, symbols, failures={Symbol.ETHUSDT})

    asyncio.run(app.cycle())

    assert app.symbol_cycle_metrics["BTCUSDT"]["cycles_succeeded"] == 1
    assert app.symbol_cycle_metrics["ETHUSDT"]["cycles_failed"] == 1
    assert app.symbol_cycle_metrics["SOLUSDT"]["cycles_succeeded"] == 1
    incident = next(iter(repo.incidents.values()))
    assert incident.payload["message"] == "required market decimal missing"
    assert incident.payload["processing_stage"] == "feature_generation"
    assert incident.payload["symbol"] == "ETHUSDT"


def test_strategy_failure_does_not_block_other_strategy(tmp_path) -> None:
    app, repo, _ = runtime(tmp_path, (Symbol.BTCUSDT,))
    strategies = list(build_v2_strategies(app.settings))
    failing = next(row for row in strategies if row.name == StrategyName.OI_FUNDING_SQUEEZE)
    succeeding = next(row for row in strategies if row.name == StrategyName.VOLUME_BREAKOUT)
    failing.evaluate = lambda features, **context: (_ for _ in ()).throw(ValueError("bad OI"))  # type: ignore[method-assign]
    app.strategies = (failing, succeeding)

    asyncio.run(app.cycle())

    assert any(item.strategy_name == StrategyName.VOLUME_BREAKOUT for item in repo.signals)
    assert any(
        item.payload["strategy"] == StrategyName.OI_FUNDING_SQUEEZE.value
        for item in repo.incidents.values()
    )


def test_repeated_failures_are_fingerprinted_and_counted(tmp_path) -> None:
    app, repo, _ = runtime(tmp_path, (Symbol.BTCUSDT,))

    def record() -> None:
        try:
            raise ValueError("same deterministic failure")
        except ValueError as exc:
            app._record_failure(
                exc, stage="feature_generation", cycle_id="c",
                symbol=Symbol.BTCUSDT,
            )

    record(); record()

    assert len(repo.incidents) == 1
    assert next(iter(repo.incidents.values())).payload["occurrence_count"] == 2


def test_meme_strategy_scope_is_configuration_driven(tmp_path) -> None:
    configured = settings(tmp_path, v2_meme_trend_symbols=("DOGEUSDT", "WIFUSDT"))
    meme = next(
        row for row in build_v2_strategies(configured)
        if row.name == StrategyName.MEME_TREND
    )

    assert meme.applies_to(Symbol.BTCUSDT) is False
    assert meme.applies_to(Symbol.DOGEUSDT) is True
    assert meme.applies_to(Symbol.WIFUSDT) is True
    assert next(
        row for row in build_v2_strategies(configured)
        if row.name == StrategyName.VOLUME_BREAKOUT
    ).applies_to(Symbol.BTCUSDT) is True
    market_scoped = settings(
        tmp_path, v2_market_strategy_symbols=("ETHUSDT",)
    )
    volume = next(
        row for row in build_v2_strategies(market_scoped)
        if row.name == StrategyName.VOLUME_BREAKOUT
    )
    assert volume.applies_to(Symbol.BTCUSDT) is False
    assert volume.applies_to(Symbol.ETHUSDT) is True


def test_news_momentum_connected_and_news_audit_files_generated(tmp_path) -> None:
    item = NewsItem(
        title="SEC approves Bitcoin ETF", summary="BlackRock BTC ETF approval",
        source="mock-rss", url="https://example.invalid/news",
        published_at=datetime.now(timezone.utc),
    )
    symbols = EntityMapper().symbols_for_text(f"{item.title} {item.summary}")
    aggregator = Aggregator([(item, symbols, "fingerprint")])
    app, repo, _ = runtime(tmp_path, (Symbol.BTCUSDT,), aggregator=aggregator)

    asyncio.run(app.cycle())
    report = V2ReportGenerator(repo, str(tmp_path)).generate("runtime-test")

    assert app.strategy_evaluation_counts[StrategyName.NEWS_MOMENTUM_V2.value] >= 1
    assert app.news_metrics["news_momentum_candidates_generated"] == 1
    assert Path(report["artifact_directory"], "news_items.csv").exists()
    assert Path(report["artifact_directory"], "news_decisions.csv").exists()
    assert Path(report["artifact_directory"], "news_sources.json").exists()


def _report_repo(runtime_payload, incidents=None, signals=None):
    return SimpleNamespace(v2_report_rows=lambda run_id: {
        "signals": signals or [], "rejections": signals or [],
        "incidents": incidents or [], "executions": [], "runtime": runtime_payload,
    })


def test_functional_result_fails_for_cycle_failure_or_skipped_symbols(tmp_path) -> None:
    incident = {
        "id": "failure", "run_id": "r", "event_type": "V2_CYCLE_FAILURE",
        "payload": {
            "occurrence_count": 4, "traceback_fingerprint": "fp",
            "message": "failure", "processing_stage": "feature_generation",
            "transient": False,
        },
    }
    runtime_payload = {
        "accepted_symbols": ["BTCUSDT", "ETHUSDT"],
        "symbol_cycle_metrics": {
            "BTCUSDT": {"cycles_attempted": 1, "cycles_succeeded": 1, "cycles_failed": 0}
        },
        "enabled_strategies": ["VolumeBreakoutStrategy"],
        "strategy_evaluation_counts": {"VolumeBreakoutStrategy": 1},
        "cycle_failure_repeat_limit": 3,
    }

    report = V2ReportGenerator(
        _report_repo(runtime_payload, [incident]), str(tmp_path)
    ).generate("r")

    assert report["functional_result"] == "FAIL"
    assert "accepted symbols were never successfully processed" in report["functional_blockers"]


def test_zero_trades_with_healthy_cycles_is_functional_pass(tmp_path) -> None:
    runtime_payload = {
        "accepted_symbols": ["BTCUSDT"],
        "symbol_cycle_metrics": {
            "BTCUSDT": {"cycles_attempted": 2, "cycles_succeeded": 2, "cycles_failed": 0}
        },
        "enabled_strategies": ["VolumeBreakoutStrategy"],
        "strategy_evaluation_counts": {"VolumeBreakoutStrategy": 2},
    }

    report = V2ReportGenerator(
        _report_repo(runtime_payload), str(tmp_path)
    ).generate("r")

    assert report["functional_result"] == "PASS"
    assert report["completed_trades"] == 0


def test_rejections_include_diagnostic_score_fields(tmp_path) -> None:
    signal = {
        "run_id": "r", "id": "candidate", "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy_name": "VolumeBreakoutStrategy", "symbol": "BTCUSDT", "side": "LONG",
        "raw_strategy_score": "0.4", "score_components": {"final_score": "0.3"},
        "threshold": "0.62", "distance_to_threshold": "-0.32",
        "estimated_edge_bps": "4", "state": "REJECTED",
        "rejection_reason": "below threshold",
    }
    runtime_payload = {
        "accepted_symbols": [], "symbol_cycle_metrics": {},
        "enabled_strategies": [], "strategy_evaluation_counts": {},
    }

    report = V2ReportGenerator(
        _report_repo(runtime_payload, signals=[signal]), str(tmp_path)
    ).generate("r")
    text = Path(report["artifact_directory"], "rejections.csv").read_text(
        encoding="utf-8-sig"
    )

    for field in (
        "side", "raw_score", "final_score", "threshold",
        "distance_to_threshold", "estimated_edge_bps", "state", "rejection_reason",
    ):
        assert field in text.splitlines()[0]


def test_runner_resolves_host_database_and_reports_functional_result() -> None:
    text = Path("scripts/demo_v2_soak.ps1").read_text(encoding="utf-8")
    assert "Get-HostPostgresPort" in text
    assert "Set-HostDatabaseEnvironment" in text
    assert "@127.0.0.1:$Port/" in text
    assert "ALEMBIC: START" in text
    assert "READ-ONLY PREFLIGHT: START" in text
    assert "UVICORN: START" in text
    assert "$report.functional_result" in text
