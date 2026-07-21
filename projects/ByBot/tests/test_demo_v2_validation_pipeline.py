from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.bybit.demo import DemoExecutionService
from app.bybit.demo_diagnostics import DemoDiagnosticsConfig
from app.models import DemoExecutionState
from app.v2.run_analysis import analyze_demo_v2_run
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


def test_runner_finally_stops_uvicorn_and_prevents_duplicate_listener() -> None:
    source = Path("scripts/demo_v2_soak.ps1").read_text(encoding="utf-8")
    assert "Assert-SingleUvicornOwner" in source
    finally_body = source.rsplit("} finally {", 1)[1]
    assert "Stop-Uvicorn" in finally_body
    assert "analyze_demo_v2_run.py" in source
