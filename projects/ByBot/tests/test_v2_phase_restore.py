from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models import DemoExecutionState, Symbol
from app.db.persistence import PersistenceRepository
from app.v2.drain import V2DrainController, V2RunPhase
from app.v2.runtime import V2Runtime
from tests.test_v2_finalization_safety import execution
from tests.test_v2_runtime_observability import (
    Aggregator,
    Execution,
    Features,
    MemoryRepository,
    Portfolio,
    Universe,
    settings,
)


class RestoreRepository(MemoryRepository):
    def __init__(
        self,
        *,
        phase: V2RunPhase,
        nominal_end_at: datetime | None,
        drain_started_at: datetime | None,
        started_at: datetime,
        records: list[object] | None = None,
        row_status: str | None = None,
    ) -> None:
        super().__init__()
        self.records = list(records or [])
        self.runtime = {
            "run_finalization": {
                "phase": phase.value,
                "nominal_end_at": (
                    nominal_end_at.isoformat() if nominal_end_at else None
                ),
                "drain_started_at": (
                    drain_started_at.isoformat() if drain_started_at else None
                ),
                "active_execution_ids": [
                    str(item.id)
                    for item in self.records
                    if item.state == DemoExecutionState.DEMO_POSITION_OPEN
                ],
            },
            "updated_at": (started_at + timedelta(seconds=10)).isoformat(),
        }
        self.run_state = {
            "run_id": "runtime-test",
            "started_at": started_at.isoformat(),
            "finished_at": None,
            "status": row_status or phase.value,
            "runtime": self.runtime,
        }

    def load_v2_run_state(self, _run_id):
        return self.run_state

    def load_demo_executions(self):
        return list(self.records)


def restored_runtime(
    tmp_path: Path,
    *,
    phase: V2RunPhase,
    nominal_end_at: datetime | None = None,
    drain_started_at: datetime | None = None,
    active: bool = False,
    authoritative: bool = True,
    remote_positions: int = 0,
    remote_orders: int = 0,
    row_status: str | None = None,
) -> tuple[V2Runtime, RestoreRepository, Execution]:
    started_at = datetime(2026, 7, 25, 10, tzinfo=timezone.utc)
    records = []
    if active:
        item = execution(Symbol.XRPUSDT, quantity="91.7")
        item.run_id = "runtime-test"
        records.append(item)
    repository = RestoreRepository(
        phase=phase,
        nominal_end_at=nominal_end_at,
        drain_started_at=drain_started_at,
        started_at=started_at,
        records=records,
        row_status=row_status,
    )
    exchange = Execution()
    exchange.demo_execution.as_status = lambda: {
        "remote_state_authoritative": authoritative,
        "bot_owned_open_positions": remote_positions,
        "bot_owned_open_orders": remote_orders,
        "unrelated_open_orders": 0,
    }
    runtime = V2Runtime(
        settings(tmp_path),
        repository,
        Universe((Symbol.XRPUSDT,)),
        Features(),
        Aggregator(),
        SimpleNamespace(
            items=[],
            real_llm_calls_count=0,
            classifier_metrics_payload=lambda: {},
        ),
        Portfolio(),
        exchange,
        None,
        run_id="runtime-test",
    )
    runtime._last_rest_poll_at = datetime.now(timezone.utc)
    return runtime, repository, exchange


def test_running_before_deadline_restores_running() -> None:
    now = datetime(2026, 7, 25, 10, tzinfo=timezone.utc)
    controller = V2DrainController(
        now + timedelta(hours=1),
        lead_seconds=300,
        timeout_seconds=900,
        restored_phase=V2RunPhase.RUNNING,
        now=now,
    )

    assert controller.phase == V2RunPhase.RUNNING


def test_running_after_deadline_restores_draining() -> None:
    now = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
    nominal = now - timedelta(seconds=1)
    controller = V2DrainController(
        nominal,
        lead_seconds=300,
        timeout_seconds=900,
        restored_phase=V2RunPhase.RUNNING,
        now=now,
    )

    assert controller.phase == V2RunPhase.DRAINING
    assert controller.drain_started_at == nominal - timedelta(seconds=300)


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (V2RunPhase.DRAINING, V2RunPhase.DRAINING),
        (V2RunPhase.RECONCILING, V2RunPhase.RECONCILING),
        (V2RunPhase.FINISHED, V2RunPhase.FINISHED),
    ],
)
def test_non_running_phase_never_regresses(
    tmp_path: Path, phase: V2RunPhase, expected: V2RunPhase
) -> None:
    runtime, _, _ = restored_runtime(
        tmp_path,
        phase=phase,
        active=phase != V2RunPhase.FINISHED,
        authoritative=True,
        remote_positions=1 if phase != V2RunPhase.FINISHED else 0,
    )

    assert runtime.drain.phase == expected
    assert runtime.stop_new_entries is True


@pytest.mark.parametrize(
    "phase", [V2RunPhase.DRAINING, V2RunPhase.RECONCILING]
)
def test_flat_restored_finalization_advances_to_finished(
    tmp_path: Path, phase: V2RunPhase
) -> None:
    runtime, repository, _ = restored_runtime(tmp_path, phase=phase)

    runtime.start()

    assert runtime.status()["run_phase"] == V2RunPhase.FINISHED.value
    assert repository.runtime["run_finalization"]["phase"] == "FINISHED"
    audit = next(
        item for item in repository.incidents.values()
        if item.event_type == "V2_RUN_PHASE_RESTORED"
    )
    assert audit.payload["persisted_phase"] == phase.value
    assert audit.payload["entries_enabled"] is False
    assert audit.payload["finalization_result"] == "FINISHED"


def test_restart_preserves_run_start_deadline_and_drain_marker(
    tmp_path: Path,
) -> None:
    nominal = datetime.now(timezone.utc) + timedelta(hours=1)
    drained = datetime.now(timezone.utc) - timedelta(minutes=1)
    runtime, _, _ = restored_runtime(
        tmp_path,
        phase=V2RunPhase.DRAINING,
        nominal_end_at=nominal,
        drain_started_at=drained,
        active=True,
        remote_positions=1,
    )

    assert runtime.run_id == "runtime-test"
    assert runtime.started_at == datetime(
        2026, 7, 25, 10, tzinfo=timezone.utc
    )
    assert runtime.drain.nominal_end_at == nominal
    assert runtime.drain.drain_started_at == drained


@pytest.mark.parametrize(
    "phase",
    [
        V2RunPhase.DRAINING,
        V2RunPhase.RECONCILING,
        V2RunPhase.FINISHED,
    ],
)
def test_restored_finalization_phase_admits_and_submits_no_entries(
    tmp_path: Path, phase: V2RunPhase
) -> None:
    runtime, repository, exchange = restored_runtime(
        tmp_path,
        phase=phase,
        active=phase != V2RunPhase.FINISHED,
        remote_positions=1 if phase != V2RunPhase.FINISHED else 0,
    )

    asyncio.run(runtime.cycle())

    assert runtime.execution_entries_allowed() is False
    assert repository.signals == []
    assert exchange.calls == 0


def test_stale_running_runtime_cannot_resurrect_finished_run(
    tmp_path: Path,
) -> None:
    runtime, _, _ = restored_runtime(
        tmp_path,
        phase=V2RunPhase.RUNNING,
        row_status=V2RunPhase.FINISHED.value,
    )

    assert runtime.drain.phase == V2RunPhase.FINISHED
    assert runtime.execution_entries_allowed() is False


def test_repeated_finished_restart_is_idempotent(tmp_path: Path) -> None:
    runtime, repository, _ = restored_runtime(
        tmp_path, phase=V2RunPhase.FINISHED
    )

    runtime.start()
    first_incident_count = len(repository.incidents)
    runtime.start()

    assert runtime.status()["run_phase"] == "FINISHED"
    assert len(repository.incidents) == first_incident_count


def test_persistence_rejects_phase_regression_and_preserves_run_boundary(
    tmp_path: Path,
) -> None:
    repository = PersistenceRepository(
        f"sqlite:///{tmp_path / 'phase.db'}"
    )
    started_at = datetime(2026, 7, 25, 10, tzinfo=timezone.utc)
    assert repository.begin_v2_run("phase-run", started_at)
    assert repository.update_v2_run_runtime(
        "phase-run", {"run_finalization": {"phase": "DRAINING"}}
    )
    assert repository.update_v2_run_runtime(
        "phase-run", {"run_finalization": {"phase": "RUNNING"}}
    )

    state = repository.load_v2_run_state("phase-run")

    assert state["started_at"] == started_at.isoformat()
    assert state["status"] == "DRAINING"
    assert state["runtime"]["run_finalization"]["phase"] == "DRAINING"

    assert repository.finish_v2_run("phase-run", {"result": "PASS"})
    assert repository.update_v2_run_runtime(
        "phase-run", {"run_finalization": {"phase": "RUNNING"}}
    )
    finished = repository.load_v2_run_state("phase-run")
    assert finished["status"] == "FINISHED"
    assert finished["runtime"]["run_finalization"]["phase"] == "FINISHED"
