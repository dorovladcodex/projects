from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.bybit.demo_diagnostics import (
    DEMO_READ_ONLY_REST_URL,
    DemoDiagnosticsConfig,
    ReadOnlyBybitDemoClient,
    evaluate_demo_recovery_readiness,
    run_demo_diagnostics,
    validate_recoverable_demo_latch,
)
from app.db.persistence import PersistenceRepository
from app.models import (
    DemoExecutionRecord,
    DemoExecutionState,
    Side,
    SignalDryRunResult,
    Symbol,
)
from tests.test_bybit_demo_execution import candidate_bundle


EXECUTION_ID = "0033f5c9-7b97-4832-92f1-6853e1e4d95f"
RECOVERABLE_REASON = "unprotected position: position update has no TP/SL"


class DiagnosticRepository:
    available = True

    def __init__(self, execution=None, *, reasons=None) -> None:
        self.execution = execution or resolved_execution()
        self.kill = {
            "active": True,
            "reasons": list(reasons or [RECOVERABLE_REASON]),
            "updated_at": datetime.now(timezone.utc),
            "activated_at": datetime.now(timezone.utc),
            "activation_count": 1,
            "events": [{
                "id": "activation-1", "event_type": "KILL_SWITCH_ACTIVATED",
                "execution_id": str(self.execution.id),
                "created_at": datetime.now(timezone.utc),
            }],
        }
        self.execution_events = []

    def load_demo_kill_switch(self):
        return self.kill

    def load_demo_executions(self):
        return [self.execution]

    def load_demo_execution_events(self, execution_id):
        return list(self.execution_events)


class DiagnosticClient:
    def __init__(self, *, orders=None, positions=None, history=None, fail=False) -> None:
        self.orders = list(orders or [])
        self.positions = dict(positions or {})
        self.fail = fail
        self.history = list(history or [])

    def verify(self):
        if self.fail:
            raise TimeoutError("read-only verification unavailable")

    def get_open_orders(self):
        return list(self.orders)

    def get_positions(self, symbol):
        size = self.positions.get(symbol.value, "0")
        return [{"symbol": symbol.value, "size": size, "side": "Buy"}]

    def get_order_history(self, symbol):
        return list(self.history)


def config() -> DemoDiagnosticsConfig:
    return DemoDiagnosticsConfig(
        database_url="sqlite://",
        api_key="fake-read-only-key",
        api_secret="fake-read-only-secret",
    )


def resolved_execution() -> DemoExecutionRecord:
    candidate, _, _, _ = candidate_bundle()
    return DemoExecutionRecord(
        id=EXECUTION_ID,
        candidate_id=candidate.id,
        risk_decision_id=3,
        run_id="demo-canary-test",
        order_link_id="bybot-abc123-e-entryhash",
        order_id="entry-id",
        close_order_link_id="bybot-abc123-e-closehash",
        close_order_id="close-id",
        state=DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE,
        symbol=Symbol.BTCUSDT,
        side=Side.BUY,
        requested_quantity=Decimal("0.001"),
        accepted_quantity=Decimal("0.001"),
        average_fill_price=Decimal("64021.8"),
        failure_reason="local position-open state timeout",
        cleanup_result="remote position flat and bot-owned orders zero",
    )


def interruption_case(*, include_flat_audit=True, open_order=False, size="0"):
    execution = resolved_execution().model_copy(update={
        "state": DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION,
        "failure_reason": "Windows sleep/resume interrupted canary client workflow",
    })
    repository = DiagnosticRepository(execution, reasons=[
        "unprotected position: position update has no TP/SL",
        "unprotected position: DemoExchangeError: Bybit Demo request failed: not modified",
        "unprotected position: DemoExchangeError: Bybit Demo request failed: can not set tp/sl/ts for zero position",
    ])
    audit = [
        "READ_ONLY_RECONCILIATION_COMPLETED", "EXECUTION_REPAIR_APPLIED",
    ]
    if include_flat_audit:
        audit.append("FINAL_REMOTE_STATE_FLAT")
    repository.execution_events = [
        {"event_type": name, "occurred_at": datetime.now(timezone.utc)}
        for name in audit
    ]
    history = [
        {"orderId": "entry-id", "orderLinkId": execution.order_link_id,
         "orderStatus": "Filled", "reduceOnly": False},
        {"orderId": "close-id", "orderLinkId": execution.close_order_link_id,
         "orderStatus": "Filled", "reduceOnly": True},
    ]
    orders = ([{"orderId": "open", "orderLinkId": execution.order_link_id}]
              if open_order else [])
    client = DiagnosticClient(
        orders=orders, positions={"BTCUSDT": size}, history=history
    )
    return execution, repository, client


def test_diagnostics_config_ignores_demo_trading_startup_flags() -> None:
    loaded = DemoDiagnosticsConfig.load({
        "DATABASE_URL": "postgresql://user:pass@db:5432/bybot",
        "BYBIT_API_KEY": "key",
        "BYBIT_API_SECRET": "secret",
        "BYBIT_PRIVATE_DEMO_BASE_URL": DEMO_READ_ONLY_REST_URL,
        "BYBIT_DEMO_TRADING_ENABLED": "false",
        "APP_ENV": "not-demo",
    }, Path("missing.env"))

    assert loaded.database_url.startswith("postgresql+psycopg://")
    assert "@127.0.0.1:" in loaded.database_url


def test_read_only_client_has_no_exchange_mutation_methods() -> None:
    client = ReadOnlyBybitDemoClient(
        "key", "secret", http_get=lambda *args: {"retCode": 0, "result": {"list": []}}
    )
    for method in (
        "create_order", "cancel_order", "amend_order", "set_trading_stop",
        "set_leverage", "close_position",
    ):
        assert not hasattr(client, method)


def test_active_recoverable_latch_with_flat_remote_state_passes() -> None:
    result = run_demo_diagnostics(
        config(), repository=DiagnosticRepository(), client=DiagnosticClient()
    )
    assert result.passed is True
    assert validate_recoverable_demo_latch(result, EXECUTION_ID) == []


def test_reset_is_refused_when_position_exists() -> None:
    result = run_demo_diagnostics(
        config(), repository=DiagnosticRepository(),
        client=DiagnosticClient(positions={"BTCUSDT": "0.001"}),
    )
    assert "Demo position exists" in " ".join(
        validate_recoverable_demo_latch(result, EXECUTION_ID)
    )


def test_reset_is_refused_for_bot_order_but_not_unrelated_order() -> None:
    bot = {"orderId": "x", "orderLinkId": "bybot-abc123-e-newhash"}
    result = run_demo_diagnostics(
        config(), repository=DiagnosticRepository(),
        client=DiagnosticClient(orders=[bot]),
    )
    assert len(result.bot_owned_open_orders) == 1
    assert validate_recoverable_demo_latch(result, EXECUTION_ID)

    unrelated = {"orderId": "manual", "orderLinkId": "manual-order"}
    safe = run_demo_diagnostics(
        config(), repository=DiagnosticRepository(),
        client=DiagnosticClient(orders=[unrelated]),
    )
    assert safe.bot_owned_open_orders == []
    assert safe.unrelated_open_orders == [unrelated]


def test_reset_is_refused_for_unresolved_execution() -> None:
    execution = resolved_execution().model_copy(
        update={"state": DemoExecutionState.DEMO_RECONCILIATION_REQUIRED}
    )
    result = run_demo_diagnostics(
        config(), repository=DiagnosticRepository(execution), client=DiagnosticClient()
    )
    assert result.unresolved_executions
    assert validate_recoverable_demo_latch(result, EXECUTION_ID)


def test_diagnostics_fails_closed_when_bybit_verification_fails() -> None:
    with pytest.raises(TimeoutError):
        run_demo_diagnostics(
            config(), repository=DiagnosticRepository(),
            client=DiagnosticClient(fail=True),
        )


@pytest.mark.parametrize("reason", [
    "maximum daily net loss reached",
    "maximum weekly net loss reached",
    "maximum paper account drawdown reached",
    "unknown exchange state",
])
def test_risk_or_unknown_state_latches_are_never_auto_recoverable(reason: str) -> None:
    result = run_demo_diagnostics(
        config(), repository=DiagnosticRepository(reasons=[reason]),
        client=DiagnosticClient(),
    )
    assert validate_recoverable_demo_latch(result, EXECUTION_ID)


def test_successful_reset_preserves_activation_reasons_and_audit(tmp_path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'reset.db'}")
    candidate, _, preview, _ = candidate_bundle()
    repository.save_signal_result(
        SignalDryRunResult(candidate=candidate, risk_preview=preview)
    )
    execution = resolved_execution().model_copy(
        update={"candidate_id": candidate.id}
    )
    assert repository.reserve_demo_execution(execution) is not None
    assert repository.save_demo_kill_switch(True, [RECOVERABLE_REASON])
    before = repository.load_demo_kill_switch()
    assert before is not None
    activation_count = before["activation_count"]
    assert repository.link_demo_kill_switch_execution(
        str(execution.id), reason="complete repair audit linkage"
    )

    assert repository.reset_demo_kill_switch(
        str(execution.id), reason="operator-confirmed flat recovery"
    )
    state = repository.load_demo_kill_switch()

    assert state is not None and state["active"] is False
    assert state["reasons"] == [RECOVERABLE_REASON]
    assert state["activation_count"] == activation_count
    assert any(
        event["event_type"] == "KILL_SWITCH_EXECUTION_LINK_REPAIRED"
        and event["execution_id"] == str(execution.id)
        for event in state["events"]
    )
    assert any(
        event["event_type"] == "KILL_SWITCH_RESET"
        and event["execution_id"] == str(execution.id)
        for event in state["events"]
    )


def test_closed_after_interruption_is_ready_after_complete_repair_audit() -> None:
    execution, repository, client = interruption_case()
    result = run_demo_diagnostics(config(), repository=repository, client=client)
    readiness = evaluate_demo_recovery_readiness(result, str(execution.id))
    assert readiness.latest_execution_terminal is True
    assert readiness.repair_audit_complete is True
    assert readiness.latest_execution_safely_closed is True
    assert readiness.recoverable_latch is True
    assert readiness.blockers == ()


def test_closed_after_interruption_requires_final_flat_audit() -> None:
    execution, repository, client = interruption_case(include_flat_audit=False)
    result = run_demo_diagnostics(config(), repository=repository, client=client)
    readiness = evaluate_demo_recovery_readiness(result, str(execution.id))
    assert readiness.repair_audit_complete is False
    assert "interruption repair audit is incomplete" in readiness.blockers


@pytest.mark.parametrize("open_order,size", [(True, "0"), (False, "0.001")])
def test_closed_after_interruption_requires_remote_flat_and_no_bot_order(
    open_order: bool, size: str
) -> None:
    execution, repository, client = interruption_case(
        open_order=open_order, size=size
    )
    result = run_demo_diagnostics(config(), repository=repository, client=client)
    readiness = evaluate_demo_recovery_readiness(result, str(execution.id))
    assert readiness.recoverable_latch is False
    assert readiness.blockers


def test_unknown_protection_error_is_not_recoverable() -> None:
    execution, repository, client = interruption_case()
    repository.kill["reasons"] = [
        "unprotected position: unexpected exchange protection failure"
    ]
    result = run_demo_diagnostics(config(), repository=repository, client=client)
    readiness = evaluate_demo_recovery_readiness(result, str(execution.id))
    assert readiness.reason_classification == "unknown_protection_incident"
    assert readiness.recoverable_latch is False
