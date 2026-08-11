from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from threading import Event, Thread
import time

import pytest

from app.config import Settings
from app.microstructure.calculations import (
    TOUCH_HORIZONS_SECONDS,
    build_carry_candidate,
    build_leg_snapshot,
    evaluate_hypothetical_touch,
    hypothetical_quotes,
    synchronize_snapshot,
)
from app.microstructure.collector import (
    CollectorConfiguration,
    MicrostructureCollector,
    StageTimingMetrics,
    advance_fixed_deadline,
)
from app.microstructure.public import MicrostructureMarketState
from app.microstructure.storage import (
    MicrostructureStorage,
    _artifact_file_write_lock,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def config(tmp_path: Path, **updates: object) -> CollectorConfiguration:
    base = CollectorConfiguration.from_settings(Settings(_env_file=None), Path.cwd())
    return replace(
        base,
        artifact_dir=tmp_path,
        candidate_symbols=("BTCUSDT",),
        universe_size=1,
        notionals_usdt=(Decimal("100"),),
        **updates,
    )


def leg(category: str, at: datetime):
    ticker = {}
    if category == "linear":
        ticker = {
            "markPrice": "100.12",
            "indexPrice": "100.01",
            "fundingRate": "0.0001",
            "nextFundingTime": str(int((at + timedelta(hours=8)).timestamp() * 1000)),
        }
    return build_leg_snapshot(
        category=category,
        symbol="BTCUSDT",
        exchange_timestamp=at,
        local_receive_timestamp=at,
        bids=[(Decimal("100"), Decimal("20"))],
        asks=[(Decimal("100.2"), Decimal("20"))],
        recent_trade_price=Decimal("100.1"),
        recent_trade_volume=Decimal("1"),
        recent_trade_timestamp=at,
        ticker=ticker,
        funding_timestamp=at if category == "linear" else None,
        funding_interval_minutes=480 if category == "linear" else None,
        open_interest=Decimal("100000") if category == "linear" else None,
        open_interest_timestamp=at if category == "linear" else None,
    )


def snapshot_at(at: datetime):
    return synchronize_snapshot(
        symbol="BTCUSDT",
        spot=leg("spot", at),
        perpetual=leg("linear", at),
        completed_at=at,
        clock_offset_ms=Decimal("0"),
        max_source_age_ms=Decimal("5000"),
        max_sync_gap_ms=Decimal("2000"),
    )


def test_fixed_deadline_scheduler_skips_missed_slots_without_drift() -> None:
    assert advance_fixed_deadline(100.0, 100.1, 10.0) == 110.0
    assert advance_fixed_deadline(100.0, 135.0, 10.0) == 140.0
    assert advance_fixed_deadline(100.0, 140.0, 10.0) == 150.0


def test_stage_percentiles_interpolate_tail_outlier_honestly() -> None:
    metrics = StageTimingMetrics()
    for value in [10.0] * 28 + [20.0]:
        metrics.record("capture_start_interval_ms", value)
    row = metrics.snapshot()["capture_start_interval_ms"]
    assert row["p95"] == 10.0
    assert row["p99"] == pytest.approx(17.2)
    assert row["max"] == 20.0


def test_bounded_worker_wakeup_coalesces_without_unbounded_tasks(tmp_path: Path) -> None:
    collector = MicrostructureCollector(config(tmp_path))

    async def scenario() -> None:
        collector._worker_queues = {  # noqa: SLF001 - scheduling contract test
            name: asyncio.Queue(maxsize=1)
            for name in ("maker", "label", "maintenance")
        }
        collector._signal_worker("maker")  # noqa: SLF001
        collector._signal_worker("maker")  # noqa: SLF001
        status = collector._research_worker_status()  # noqa: SLF001
        assert status["maker"]["queue_depth"] == 1
        assert status["maker"]["queue_capacity"] == 1
        assert status["maker"]["coalesced_wakeups"] == 1
        assert status["maker"]["durable_source_of_truth"] is True

    asyncio.run(scenario())
    collector.close()


def test_maker_worker_loads_each_grouped_path_once_and_only_due_horizons(
    tmp_path: Path,
) -> None:
    storage = MicrostructureStorage(tmp_path)
    row = snapshot_at(NOW)
    storage.save_capture(row)
    quotes = hypothetical_quotes(row)
    for quote in quotes:
        storage.save_quote(quote)
    state = MicrostructureMarketState()
    collector = MicrostructureCollector(config(tmp_path), storage=storage, state=state)
    path_calls: list[tuple[str, str]] = []
    capture_path_calls: list[tuple[str, str]] = []

    def quote_paths(category: str, symbol: str):
        path_calls.append((category, symbol))
        mids = [(NOW + timedelta(seconds=value), Decimal("100.1")) for value in range(1, 66)]
        return [], mids

    def stored_paths(
        symbol: str,
        venue_leg: str,
        start: datetime,
        end: datetime,
        *,
        storage: MicrostructureStorage | None = None,
    ):
        capture_path_calls.append((symbol, venue_leg))
        return [], []

    state.quote_paths = quote_paths  # type: ignore[method-assign]
    collector._paths_from_captures = stored_paths  # type: ignore[method-assign]
    collector.evaluate_maker_telemetry(evaluated_at=NOW + timedelta(seconds=6))

    assert sorted(path_calls) == [("linear", "BTCUSDT"), ("spot", "BTCUSDT")]
    assert sorted(capture_path_calls) == [
        ("BTCUSDT", "perpetual"),
        ("BTCUSDT", "spot"),
    ]
    with storage._lock:  # noqa: SLF001 - assert durable worker output
        horizons = {
            int(row[0])
            for row in storage.connection.execute(
                "SELECT DISTINCT horizon_seconds FROM hypothetical_touch_outcomes"
            ).fetchall()
        }
    assert horizons == {1, 5}
    collector.close()


def test_completed_maker_horizon_is_not_reprocessed(tmp_path: Path) -> None:
    storage = MicrostructureStorage(tmp_path)
    quote = hypothetical_quotes(snapshot_at(NOW))[0]
    storage.save_quote(quote)
    completed = evaluate_hypothetical_touch(
        quote,
        horizon_seconds=1,
        evaluated_at=NOW + timedelta(seconds=2),
        trades=[],
        midpoints=[(NOW + timedelta(seconds=1), Decimal("100.1"))],
    )
    assert completed.complete is True
    storage.save_touch_outcome(completed)
    work = storage.pending_maker_work(
        evaluated_at=NOW + timedelta(seconds=2),
        horizons_seconds=TOUCH_HORIZONS_SECONDS,
    )
    assert (quote.quote_id, 1) not in {(row.quote_id, horizon) for row, horizon in work}
    storage.close()


def test_future_label_schedule_filters_immature_rows_before_parsing(tmp_path: Path) -> None:
    storage = MicrostructureStorage(tmp_path)
    candidate, _ = build_carry_candidate(
        snapshot_at(NOW),
        notionals=(Decimal("100"),),
        account_fees_bps={
            "spot_maker": Decimal("10"),
            "spot_taker": Decimal("10"),
            "perp_maker": Decimal("2"),
            "perp_taker": Decimal("5.5"),
        },
    )
    assert storage.save_carry_candidate(candidate) is True
    immature = storage.due_opportunities_missing_label(
        horizon="12h",
        notional_usdt=Decimal("100"),
        evaluated_at=NOW + timedelta(hours=11, minutes=59),
        fixed_delta=timedelta(hours=12),
    )
    mature = storage.due_opportunities_missing_label(
        horizon="12h",
        notional_usdt=Decimal("100"),
        evaluated_at=NOW + timedelta(hours=12),
        fixed_delta=timedelta(hours=12),
    )
    assert immature == []
    assert len(mature) == 1
    storage.close()


def test_future_label_worker_matures_due_horizon_once_without_lookahead(
    tmp_path: Path,
) -> None:
    storage = MicrostructureStorage(tmp_path)
    entry = snapshot_at(NOW)
    candidate, _ = build_carry_candidate(
        entry,
        notionals=(Decimal("100"),),
        account_fees_bps={
            "spot_maker": Decimal("10"),
            "spot_taker": Decimal("10"),
            "perp_maker": Decimal("2"),
            "perp_taker": Decimal("5.5"),
        },
    )
    storage.save_capture(entry)
    storage.save_carry_candidate(candidate)
    storage.save_capture(snapshot_at(NOW + timedelta(hours=8)))
    storage.save_capture(snapshot_at(NOW + timedelta(hours=12)))
    collector = MicrostructureCollector(config(tmp_path), storage=storage)
    assert collector.mature_future_labels(
        evaluated_at=NOW + timedelta(hours=7, minutes=59)
    ) == 0
    first = collector.mature_future_labels(evaluated_at=NOW + timedelta(hours=12))
    second = collector.mature_future_labels(evaluated_at=NOW + timedelta(hours=12))
    assert first >= 1
    assert second == 0
    assert storage.has_label(candidate.opportunity_id, "12h", Decimal("100"))
    collector.close()


def test_regular_user_fee_schedule_and_mnt_scenario_are_separate(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        v5_spot_maker_fee_bps="10.0",
        v5_spot_taker_fee_bps="10.0",
        v5_perp_maker_fee_bps="2.0",
        v5_perp_taker_fee_bps="5.5",
        v5_fee_source="ACCOUNT_SCREENSHOT_REGULAR_USER",
        v5_fee_schedule="BASE_REGULAR_USER",
        v5_mnt_discount_scenario_enabled=False,
    )
    configured = CollectorConfiguration.from_settings(settings, Path.cwd())
    payload = configured.cost_payload()
    assert payload["fee_source"] == "ACCOUNT_SCREENSHOT_REGULAR_USER"
    assert payload["fee_schedule"] == "BASE_REGULAR_USER"
    assert payload["fees"] == {
        "spot_maker_fee_bps": "10.0",
        "spot_taker_fee_bps": "10.0",
        "perp_maker_fee_bps": "2.0",
        "perp_taker_fee_bps": "5.5",
    }
    scenario = payload["offline_research_scenarios"]["MNT_DISCOUNT_SCENARIO"]
    assert scenario["enabled_for_offline_research"] is False
    assert scenario["authoritative_account_rate"] is False
    assert configured.account_fees_bps["perp_taker"] == Decimal("5.5")


def test_capture_worker_is_independent_of_slow_research_worker(tmp_path: Path) -> None:
    collector = MicrostructureCollector(
        config(tmp_path, capture_cadence_seconds=0.02, rest_refresh_seconds=60)
    )
    collector.symbols = ("BTCUSDT",)

    def capture_once() -> dict[str, int]:
        collector.capture_cycles += 1
        return {"captures": 1, "complete": 1, "costs": 0, "quotes": 0}

    collector.capture_once = capture_once  # type: ignore[method-assign]

    async def scenario() -> float:
        collector._worker_queues = {  # noqa: SLF001
            name: asyncio.Queue(maxsize=1)
            for name in ("maker", "label", "maintenance")
        }
        stop = asyncio.Event()
        started = perf_counter()
        slow_research = asyncio.create_task(asyncio.to_thread(__import__("time").sleep, 0.25))
        await collector._capture_worker(stop, max_capture_cycles=4)  # noqa: SLF001
        elapsed = perf_counter() - started
        await slow_research
        return elapsed

    assert asyncio.run(scenario()) < 0.20
    assert collector.capture_cycles == 4
    collector.close()


def test_research_connection_python_lock_cannot_block_capture_storage(
    tmp_path: Path,
) -> None:
    capture_storage = MicrostructureStorage(tmp_path)
    research_storage = MicrostructureStorage(tmp_path)
    locked = Event()

    def hold_research_lock() -> None:
        with research_storage._lock:  # noqa: SLF001 - explicit isolation contract
            locked.set()
            time.sleep(0.2)

    thread = Thread(target=hold_research_lock)
    thread.start()
    assert locked.wait(timeout=1)
    started = perf_counter()
    assert capture_storage.save_capture(snapshot_at(NOW)) is True
    elapsed = perf_counter() - started
    thread.join(timeout=1)
    assert elapsed < 0.15
    capture_storage.close()
    research_storage.close()


def test_research_artifact_append_cannot_block_capture_dataset(
    tmp_path: Path,
) -> None:
    storage = MicrostructureStorage(tmp_path)
    capture_path = storage.raw_dir / "captures" / "2026-08-11.jsonl"
    outcome_path = (
        storage.raw_dir / "hypothetical-touch-outcomes" / "2026-08-11.jsonl"
    )
    assert _artifact_file_write_lock(capture_path) is not _artifact_file_write_lock(
        outcome_path
    )
    capture_lock = _artifact_file_write_lock(capture_path)
    completed = Event()

    def write_research_artifact() -> None:
        storage._write_raw(  # noqa: SLF001 - exercise the per-file lock contract
            "hypothetical-touch-outcomes", {"complete": True}, NOW
        )
        completed.set()

    with capture_lock:
        thread = Thread(target=write_research_artifact)
        thread.start()
        assert completed.wait(timeout=1)
    thread.join(timeout=1)
    storage.close()


def test_artifact_appends_reuse_flushed_handle_until_storage_close(
    tmp_path: Path,
) -> None:
    storage = MicrostructureStorage(tmp_path)
    path = storage.raw_dir / "captures" / "2026-08-11.jsonl"
    storage._write_raw("captures", {"sequence": 1}, NOW)  # noqa: SLF001
    handle = storage._artifact_handles[path]  # noqa: SLF001
    storage._write_raw("captures", {"sequence": 2}, NOW)  # noqa: SLF001
    assert storage._artifact_handles[path] is handle  # noqa: SLF001
    assert path.read_text(encoding="utf-8").splitlines() == [
        '{"sequence":1}',
        '{"sequence":2}',
    ]
    storage.close()
    assert handle.closed


def test_restart_appends_new_capture_without_rewriting_history(tmp_path: Path) -> None:
    first = MicrostructureStorage(tmp_path)
    assert first.save_capture(snapshot_at(NOW)) is True
    first.close()
    second = MicrostructureStorage(tmp_path)
    assert second.save_capture(snapshot_at(NOW + timedelta(seconds=10))) is True
    assert second.data_quality()["captures_total"] == 2
    second.close()


def test_collector_still_has_no_exchange_execution_capability(tmp_path: Path) -> None:
    collector = MicrostructureCollector(config(tmp_path))
    source = __import__("inspect").getsource(MicrostructureCollector)
    assert collector.client.exchange_mutation_capable is False
    for forbidden in ("create_order", "place_order", "cancel_order", "amend_order"):
        assert forbidden not in source
    collector.close()
