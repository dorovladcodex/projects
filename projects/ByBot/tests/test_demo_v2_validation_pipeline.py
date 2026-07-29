from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.bybit.demo import DemoExecutionService
from app.bybit.demo_diagnostics import DemoDiagnosticsConfig
from app.models import DemoExecutionState
from app.v2.run_analysis import _StableReadSnapshotClient, analyze_demo_v2_run
from tests.test_bybit_demo_execution import FakeDemoClient, MemoryRepository, demo_settings
from tests.test_demo_execution_recovery import ReadClient, record
from tests.test_v2_completed_run_regressions import _close_payload, _closing_record
from tests.test_v2_runtime_observability import runtime


class AnalysisRepository:
    available = True

    def __init__(self, execution) -> None:
        self.execution = execution

    def load_demo_executions(self):
        return [self.execution]

    def load_demo_execution_events(self, execution_id):
        return [{
            "event_type": "ENTRY_ACKNOWLEDGED",
            "occurred_at": self.execution.created_at.isoformat(),
            "state": self.execution.state.value,
        }]

    def load_demo_kill_switch(self):
        return {
            "active": False, "reasons": [], "events": [],
            "activation_count": 0,
        }


def _analysis_config() -> DemoDiagnosticsConfig:
    return DemoDiagnosticsConfig("sqlite://", "fake-key", "fake-secret")


def test_warning_and_hard_failure_are_bounded_and_persisted() -> None:
    repository = MemoryRepository()
    item = _closing_record()
    repository.records[str(item.candidate_id)] = item
    client = FakeDemoClient()
    order, fill = _close_payload(item)
    client.history = [order]
    client.executions = [fill]
    service = DemoExecutionService(
        demo_settings(
            v2_terminalization_warning_seconds=30,
            v2_terminalization_hard_failure_seconds=120,
        ),
        repository,
        client,
        run_id=item.run_id,
    )
    evidence_at = datetime.now(timezone.utc)
    item.closed_at = evidence_at

    service._record_terminalization_invariant(
        item, realtime=[], history=[order], executions=[fill],
        positions=[], now=evidence_at + timedelta(seconds=30),
    )
    assert item.terminalization_warning_at is not None
    assert item.terminalization_hard_failure_at is None
    assert repository.saved_events[-1][0] == "CLOSE_TERMINALIZATION_WARNING"

    service._record_terminalization_invariant(
        item, realtime=[], history=[order], executions=[fill],
        positions=[], now=evidence_at + timedelta(seconds=120),
    )
    assert item.terminalization_hard_failure_at is not None
    assert service.terminalization_hard_failures[str(item.id)]
    assert repository.saved_events[-1][0] == "CLOSE_TERMINALIZATION_HARD_FAILURE"


def test_runtime_hard_failure_stops_entries_and_persists_once(tmp_path: Path) -> None:
    app, repo, _ = runtime(tmp_path, ())
    execution_id = "9452d339-953c-4475-bdd4-ddb940330f9f"
    app.execution.demo_execution.retry_stuck_terminalizations = lambda: {
        "hard_failures": {execution_id: ["atomic terminal persistence did not complete"]},
        "exchange_mutations_performed": False,
    }

    app.sync_terminal_executions()
    app.sync_terminal_executions()

    assert app.stop_new_entries is True
    assert app.run_valid is False
    assert len(app.run_invalid_reasons) == 1
    incidents = [
        item for item in repo.incidents.values()
        if item.event_type == "V2_TERMINALIZATION_HARD_FAILURE"
    ]
    assert len(incidents) == 1
    assert incidents[0].payload["reconciliation_continues"] is True


def test_supervisor_pause_preserves_phase_and_position_management(
    tmp_path: Path,
) -> None:
    app, _, _ = runtime(tmp_path, ())
    phase = app.status()["run_phase"]

    paused = app.set_supervisor_entries_paused(
        True, reason="bounded status fallback"
    )
    assert paused["run_phase"] == phase
    assert paused["existing_position_management_active"] is True
    assert app.entries_paused is True
    assert app.stop_new_entries is False

    resumed = app.set_supervisor_entries_paused(False)
    assert resumed["run_phase"] == phase
    assert app.entries_paused is False


def test_supervisor_terminal_sync_runs_reconcile_ledger_and_capacity_once(
    tmp_path: Path,
) -> None:
    app, _, _ = runtime(tmp_path, ())
    calls = {"invariants": 0, "reservations": 0}
    app._enforce_terminalization_invariants = lambda: calls.__setitem__(
        "invariants", calls["invariants"] + 1
    )
    app._sync_reservations = lambda: calls.__setitem__(
        "reservations", calls["reservations"] + 1
    )

    app.sync_terminal_executions()

    assert calls == {"invariants": 1, "reservations": 1}


def test_reconciliation_loop_survives_one_unexpected_failure(monkeypatch) -> None:
    import app.main as main_app

    calls = {"sleep": 0, "reconcile": 0, "sync": 0}

    async def controlled_sleep(seconds: float) -> None:
        calls["sleep"] += 1
        if calls["sleep"] >= 3:
            raise asyncio.CancelledError

    def reconcile() -> None:
        calls["reconcile"] += 1
        if calls["reconcile"] == 1:
            raise RuntimeError("one reconciliation defect")

    monkeypatch.setattr(main_app.asyncio, "sleep", controlled_sleep)
    monkeypatch.setattr(
        main_app.demo_execution_service, "reconcile", reconcile
    )
    monkeypatch.setattr(
        main_app.signal_candidate_service,
        "sync_demo_states",
        lambda: calls.__setitem__("sync", calls["sync"] + 1),
    )
    monkeypatch.setattr(
        main_app.v2_runtime,
        "sync_terminal_executions",
        lambda: calls.__setitem__("sync", calls["sync"] + 1),
    )
    main_app.demo_execution_service.reconciler_failure_count = 0

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main_app.demo_reconciliation_loop())

    assert calls["reconcile"] == 2
    assert calls["sync"] >= 1
    assert main_app.demo_execution_service.reconciler_failure_count == 1
    assert main_app.demo_execution_service.reconciler_last_error is None
    assert main_app.demo_execution_service.reconciler_task_alive is False


def test_analyzer_zero_unresolved_is_read_only(tmp_path: Path) -> None:
    item = record().model_copy(update={
        "run_id": "analysis-pass",
        "state": DemoExecutionState.DEMO_CLOSED,
        "close_order_id": "external-close",
        "close_reason": "take_profit",
        "exit_attribution": "take_profit",
        "realized_exchange_pnl": Decimal("0.07"),
    })
    repo = AnalysisRepository(item)
    result = analyze_demo_v2_run(
        "analysis-pass", _analysis_config(), repository=repo,
        client=ReadClient(close_kind="take_profit"), artifact_root=tmp_path,
    )

    assert result["analysis_result"] == "PASS"
    assert result["completed_trades"] == 1
    assert result["mutation_attempted"] is False
    assert (tmp_path / "analysis-pass" / "analysis" / "analysis.md").exists()


def test_analyzer_identifies_one_unresolved_without_mutation(tmp_path: Path) -> None:
    item = record().model_copy(update={"run_id": "analysis-unresolved"})
    repo = AnalysisRepository(item)
    result = analyze_demo_v2_run(
        "analysis-unresolved", _analysis_config(), repository=repo,
        client=ReadClient(close_kind="take_profit"), artifact_root=tmp_path,
    )

    assert result["analysis_result"] == "FAIL"
    assert result["unresolved_execution_ids"] == [str(item.id)]
    assert result["unresolved"][0]["read_only_repair_eligible"] is True
    assert result["mutation_attempted"] is False


def test_final_analysis_reuses_stable_read_only_exchange_snapshots() -> None:
    class CountingReadClient(ReadClient):
        def __init__(self) -> None:
            super().__init__(close_kind="take_profit")
            self.calls: dict[str, int] = {}

        def _count(self, name: str) -> None:
            self.calls[name] = self.calls.get(name, 0) + 1

        def verify(self):
            self._count("verify")

        def get_order_history(self, symbol):
            self._count("history")
            return super().get_order_history(symbol)

        def get_executions(self, symbol):
            self._count("executions")
            return super().get_executions(symbol)

        def get_positions(self, symbol):
            self._count("positions")
            return super().get_positions(symbol)

        def get_open_orders(self):
            self._count("orders")
            return super().get_open_orders()

        def get_closed_pnl(self, symbol):
            self._count("pnl")
            return super().get_closed_pnl(symbol)

        def get_transaction_log(self):
            self._count("transactions")
            return super().get_transaction_log()

    raw = CountingReadClient()
    cached = _StableReadSnapshotClient(raw, ("BTCUSDT",))
    cached.verify()
    cached.verify()
    for _ in range(3):
        cached.get_order_history(record().symbol)
        cached.get_executions(record().symbol)
        cached.get_closed_pnl(record().symbol)
        cached.get_transaction_log()
    for _ in range(4):
        cached.get_positions(record().symbol)
        cached.get_open_orders()

    assert raw.calls == {
        "verify": 1,
        "history": 1,
        "executions": 1,
        "pnl": 1,
        "transactions": 1,
        "positions": 2,
        "orders": 2,
    }


def test_runner_finally_stops_uvicorn_and_prevents_duplicate_listener() -> None:
    source = Path("scripts/demo_v2_soak.ps1").read_text(encoding="utf-8")
    assert "Assert-SingleUvicornOwner" in source
    finally_body = source.rsplit("} finally {", 1)[1]
    assert "Stop-Uvicorn" in finally_body
    assert "analyze_demo_v2_run.py" in source
