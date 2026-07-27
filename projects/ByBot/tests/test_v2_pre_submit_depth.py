from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.bybit.demo import DemoSafetyError
from app.v2.analytics import V2ReportGenerator
from app.v2.execution import PreSubmitMarketRejection
from app.v2.models import ReservationState, Symbol
from app.v2.portfolio import PortfolioRiskService
from tests.test_v2_execution import (
    DemoStub,
    _admitted_candidate,
    coordinator,
    features,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "demo_replay"
    / "wif_cycle_160_final_depth.json"
)
PRICE_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "demo_replay"
    / "eth_cycle_1166_jit_price.json"
)


class GuardAwareDemo(DemoStub):
    def submit_candidate(self, candidate, preview, classification, snapshot, **kwargs):
        kwargs["pre_submit_market_guard"]()
        return super().submit_candidate(
            candidate, preview, classification, snapshot, **kwargs
        )


def _guarded_service(symbol=Symbol.WIFUSDT):
    service, repository, _ = coordinator()
    demo = GuardAwareDemo()
    service.demo_execution = demo
    service.settings.v2_min_orderbook_depth_usdt = Decimal("10000")
    service.settings.v2_min_position_notional_usdt = Decimal("100")
    candidate = _admitted_candidate(service, repository, symbol)
    return service, repository, demo, candidate


def _snapshot(
    *,
    symbol=Symbol.WIFUSDT,
    ask_depth="20000",
    bid_depth="20000",
    fresh=True,
    age_seconds=0,
    full_depth=None,
):
    item = features(symbol)
    item.ask_depth_10bps_usdt = Decimal(ask_depth)
    item.bid_depth_10bps_usdt = Decimal(bid_depth)
    if full_depth is not None:
        item.ask_depth_usdt = Decimal(full_depth)
        item.bid_depth_usdt = Decimal(full_depth)
    item.fresh = fresh
    item.timestamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    item.source_timestamps["orderbook"] = item.timestamp
    return item


def test_sufficient_fresh_depth_proceeds_to_submission() -> None:
    service, _, demo, candidate = _guarded_service(Symbol.XRPUSDT)
    service.market_snapshot_provider = lambda symbol: _snapshot(
        symbol=symbol, ask_depth="20000", bid_depth="20000"
    )

    result = service.execute(candidate)

    assert result["execution_attempted"] is True
    assert demo.exchange_mutations == 1


def test_insufficient_fresh_depth_rejects_without_submission() -> None:
    service, repository, demo, candidate = _guarded_service()
    service.market_snapshot_provider = lambda symbol: _snapshot(
        symbol=symbol, ask_depth="1500"
    )

    result = service.execute(candidate)

    assert result["rejection_code"] == "FINAL_EXECUTABLE_DEPTH_INSUFFICIENT"
    assert result["handled_pre_submit_rejection"] is True
    assert result["exchange_order_submission_invoked"] is False
    assert demo.exchange_mutations == 0
    assert demo.calls == []
    assert repository.load_demo_executions() == []
    assert candidate.pre_submit_rejection is not None
    assert candidate.pre_submit_rejection.available_depth_notional_usdt == 1500
    assert candidate.pre_submit_rejection.executable_depth_notional_usdt == 75
    assert candidate.pre_submit_rejection.minimum_notional_usdt == 100


def test_safe_notional_below_minimum_rejects_instead_of_downsizing() -> None:
    service, _, demo, candidate = _guarded_service()
    service.settings.v2_min_orderbook_depth_usdt = Decimal("0")
    service.market_snapshot_provider = lambda symbol: _snapshot(
        symbol=symbol, ask_depth="1900"
    )

    result = service.execute(candidate)

    assert result["rejection_code"] == "FINAL_EXECUTABLE_DEPTH_INSUFFICIENT"
    assert result["pre_submit_audit"]["executable_depth_notional_usdt"] == "95"
    assert demo.exchange_mutations == 0


def test_reservation_release_is_exactly_once_and_restart_safe() -> None:
    service, repository, _, candidate = _guarded_service()
    service.market_snapshot_provider = lambda symbol: _snapshot(
        symbol=symbol, ask_depth="1500"
    )

    result = service.execute(candidate)
    reservation = next(
        row for row in service.portfolio.reservations
        if str(row.id) == result["reservation_id"]
    )
    released_at = reservation.released_at

    assert result["reservation_release_result"] == "RELEASED"
    assert service.portfolio.release(
        reservation.id, activate_cooldown=False
    ) is False
    assert reservation.released_at == released_at
    restored = PortfolioRiskService(service.settings, repository)
    restored_reservation = next(
        row for row in restored.reservations if row.id == reservation.id
    )
    assert restored_reservation.state == ReservationState.RELEASED
    assert all(
        row.id != reservation.id
        for row in restored.reservations
        if row.state in restored.ACTIVE
    )


@pytest.mark.parametrize(
    ("snapshot_kwargs", "code"),
    [
        ({"fresh": False}, "PRE_SUBMIT_MARKET_DATA_STALE"),
        ({"age_seconds": 10}, "PRE_SUBMIT_MARKET_DATA_STALE"),
        (
            {"ask_depth": "0", "full_depth": "0"},
            "FINAL_EXECUTABLE_DEPTH_MISSING",
        ),
    ],
)
def test_data_quality_rejections_are_precise_and_submit_no_order(
    snapshot_kwargs, code
) -> None:
    service, _, demo, candidate = _guarded_service()
    snapshot = _snapshot(**snapshot_kwargs)
    service.market_snapshot_provider = lambda symbol: snapshot

    result = service.execute(candidate)

    assert result["rejection_code"] == code
    assert result["execution_attempted"] is False
    assert demo.exchange_mutations == 0


def test_missing_snapshot_is_precise_no_order_rejection() -> None:
    service, _, demo, candidate = _guarded_service()
    service.market_snapshot_provider = lambda symbol: None

    result = service.execute(candidate)

    assert result["rejection_code"] == "PRE_SUBMIT_MARKET_DATA_UNAVAILABLE"
    assert demo.exchange_mutations == 0


def test_malformed_negative_depth_remains_safety_failure() -> None:
    service, _, demo, candidate = _guarded_service()
    snapshot = _snapshot()
    object.__setattr__(snapshot, "ask_depth_10bps_usdt", Decimal("-1"))
    service.market_snapshot_provider = lambda symbol: snapshot

    with pytest.raises(DemoSafetyError, match="malformed"):
        service.execute(candidate)

    assert demo.exchange_mutations == 0


def test_execution_created_after_failed_gate_remains_safety_failure() -> None:
    service, repository, _, candidate = _guarded_service()
    service.market_snapshot_provider = lambda symbol: _snapshot(ask_depth="1")
    original_get = repository.get_demo_execution

    def conflicting(candidate_id):
        if candidate_id == str(candidate.id):
            return object()
        return original_get(candidate_id)

    repository.get_demo_execution = conflicting

    with pytest.raises(DemoSafetyError, match="exists after"):
        service.execute(candidate)


def test_wif_cycle_160_replay_is_expected_depth_rejection() -> None:
    replay = json.loads(FIXTURE.read_text(encoding="utf-8"))
    service, _, demo, candidate = _guarded_service()
    final_snapshot = _snapshot(
        ask_depth=replay["next_persisted_snapshot"]["ask_depth_10bps_usdt"],
        bid_depth=replay["next_persisted_snapshot"]["bid_depth_10bps_usdt"],
    )
    service.market_snapshot_provider = lambda symbol: final_snapshot

    result = service.execute(candidate)

    assert result["rejection_code"] == replay["expected_rejection_code"]
    assert result["exchange_order_submission_invoked"] is False
    assert result["reservation_release_result"] == "RELEASED"
    assert demo.exchange_mutations == 0


def test_eth_cycle_1166_price_move_is_typed_no_order_rejection() -> None:
    replay = json.loads(PRICE_FIXTURE.read_text(encoding="utf-8"))
    service, repository, demo, candidate = _guarded_service(Symbol.ETHUSDT)
    candidate.feature_snapshot.ask_price = Decimal(
        replay["original_reference_price"]
    )
    candidate.feature_snapshot.last_price = Decimal(
        replay["original_reference_price"]
    )
    snapshot = _snapshot(symbol=Symbol.ETHUSDT)
    snapshot.ask_price = Decimal(replay["replay_final_executable_price"])
    snapshot.last_price = snapshot.ask_price
    service.market_snapshot_provider = lambda symbol: snapshot

    result = service.execute(candidate)

    assert result["rejection_code"] == replay["expected_rejection_code"]
    audit = result["pre_submit_audit"]
    assert audit["original_reference_price"] == replay["original_reference_price"]
    assert audit["final_executable_price"] == replay[
        "replay_final_executable_price"
    ]
    assert Decimal(audit["price_movement_bps"]) > Decimal(
        replay["configured_tolerance_bps"]
    )
    assert result["exchange_order_submission_invoked"] is False
    assert result["reservation_release_result"] == "RELEASED"
    assert repository.load_demo_executions() == []
    assert demo.exchange_mutations == 0


def test_price_inside_tolerance_proceeds() -> None:
    service, _, demo, candidate = _guarded_service(Symbol.ETHUSDT)
    candidate.feature_snapshot.ask_price = Decimal("1920.80")
    candidate.feature_snapshot.last_price = Decimal("1920.80")
    snapshot = _snapshot(symbol=Symbol.ETHUSDT)
    snapshot.ask_price = Decimal("1922.00")
    snapshot.last_price = snapshot.ask_price
    service.market_snapshot_provider = lambda symbol: snapshot

    result = service.execute(candidate)

    assert result["execution_attempted"] is True
    assert demo.exchange_mutations == 1


def test_runtime_records_rejection_without_cycle_or_persistence_failure(
    tmp_path
) -> None:
    from tests.test_v2_runtime_observability import runtime

    app, repository, _ = runtime(tmp_path, (Symbol.WIFUSDT,))
    candidate = app.strategies[1].evaluate(features(Symbol.WIFUSDT))
    candidate.run_id = app.run_id
    candidate.admitted = False
    candidate.state = "EXECUTION_REJECTED"
    result = {
        "handled_pre_submit_rejection": True,
        "rejection_code": "FINAL_EXECUTABLE_DEPTH_INSUFFICIENT",
        "rejection_message": "final pre-submit executable depth is insufficient",
        "signal_id": str(candidate.id),
        "reservation_id": "reservation",
        "reservation_release_result": "RELEASED",
        "pre_submit_audit": {
            "requested_notional_usdt": "100",
            "executable_depth_notional_usdt": "75",
        },
        "rejected_at": datetime.now(timezone.utc).isoformat(),
    }

    async def collect():
        loop = asyncio.get_running_loop()
        first = loop.create_future()
        first.set_result(result)
        later = loop.create_future()
        later.set_result({"execution_id": "valid-later"})
        await app._collect_dispatches(
            [(candidate, first), (candidate, later)], "run:160"
        )

    asyncio.run(collect())

    assert sum(app.failure_occurrences.values()) == 0
    assert app.signal_metrics["pre_submit_rejections"] == 1
    assert app.signal_metrics["final_depth_rejections"] == 1
    assert app.signal_metrics["admitted_signals"] == 1
    assert len(repository.incidents) == 1
    incident = next(iter(repository.incidents.values()))
    assert incident.event_type == "PRE_SUBMIT_ENTRY_REJECTED"
    app._record_pre_submit_rejection(candidate, result)
    assert app.signal_metrics["pre_submit_rejections"] == 1
    assert len(repository.incidents) == 1


def test_analytics_exports_expected_pre_submit_rejection(tmp_path) -> None:
    from tests.test_v2_runtime_observability import MemoryRepository

    repository = MemoryRepository()
    service, _, _, candidate = _guarded_service()
    service.market_snapshot_provider = lambda symbol: _snapshot(
        symbol=symbol, ask_depth="1500"
    )
    service.execute(candidate)
    repository.signals = [candidate]
    repository.runtime = {
        "signal_metrics": {
            "pre_submit_rejections": 1,
            "final_depth_rejections": 1,
            "pre_submit_rejections_by_code": {
                "FINAL_EXECUTABLE_DEPTH_INSUFFICIENT": 1
            },
        }
    }

    summary = V2ReportGenerator(repository, str(tmp_path)).generate("run")

    assert summary["pre_submit_rejections"] == 1
    assert summary["final_depth_rejections"] == 1
    assert summary["total_cycle_failures"] == 0
