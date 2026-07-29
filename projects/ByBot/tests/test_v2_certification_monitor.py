from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.v2.certification_monitor import (
    CertificationMonitorHealth,
    CertificationMonitorState,
    ExecutionFallbackEvidence,
    StatusFallbackEvidence,
    classify_status_fallback,
)


NOW = datetime(2026, 7, 28, 23, 38, 8, tzinfo=timezone.utc)


def execution(
    execution_id: str = "36f3abbd-e7e1-41ab-b5df-ecd0ee9a7a5c",
    *,
    symbol: str = "SUIUSDT",
    state: str = "DEMO_CLOSING",
    open_position: bool = False,
    protected: bool = False,
    remote_flat: bool = True,
    exact_close: bool = True,
    pending: bool = False,
    residuals: int = 0,
    partial: bool = False,
    conflict: bool = False,
    error: str | None = None,
) -> ExecutionFallbackEvidence:
    return ExecutionFallbackEvidence(
        execution_id=execution_id,
        symbol=symbol,
        durable_state=state,
        remote_position_open=open_position,
        protection_confirmed=protected,
        remote_flat=remote_flat,
        exact_close_evidence=exact_close,
        close_evidence_pending=pending,
        exact_owned_residual_orders=residuals,
        partial_close=partial,
        ownership_conflict=conflict,
        evidence_error=error,
    )


def evidence(
    *executions: ExecutionFallbackEvidence,
    runner: bool = True,
    uvicorn: bool = True,
    listener: bool = True,
    persistence: bool = True,
    kill_switch: bool = False,
    unrelated_positions: int = 0,
    unrelated_orders: int = 0,
    conflicts: int = 0,
    complete: bool = True,
) -> StatusFallbackEvidence:
    return StatusFallbackEvidence(
        runner_alive=runner,
        uvicorn_alive=uvicorn,
        port_listening=listener,
        persistence_ok=persistence,
        kill_switch_active=kill_switch,
        executions=tuple(executions),
        unrelated_positions=unrelated_positions,
        unrelated_orders=unrelated_orders,
        ownership_conflicts=conflicts,
        authoritative_check_complete=complete,
    )


def record(
    monitor: CertificationMonitorHealth,
    fallback: StatusFallbackEvidence,
    now: datetime = NOW,
):
    return monitor.record_status_failure(
        now=now,
        evidence=fallback,
        error="TimeoutError: /v2/status timed out",
    )


def test_status_timeout_without_exposure_is_degraded_not_failed() -> None:
    decision = classify_status_fallback(evidence())
    assert decision.state == CertificationMonitorState.STATUS_DEGRADED
    assert not decision.escalate
    assert decision.keep_reconciler_alive


def test_open_rest_confirmed_protected_position_keeps_runtime_alive() -> None:
    item = execution(
        open_position=True, protected=True, remote_flat=False, exact_close=False
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.PROTECTED_POSITION_DEGRADED
    assert not decision.escalate


def test_open_unprotected_position_fails_fast() -> None:
    item = execution(
        open_position=True, protected=False, remote_flat=False, exact_close=False
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.FAIL_FAST
    assert decision.escalate and decision.shutdown_ready


def test_natural_close_with_durable_closing_is_terminalization_pending() -> None:
    decision = classify_status_fallback(evidence(execution()))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING
    assert decision.request_reconciliation
    assert decision.keep_reconciler_alive
    assert not decision.shutdown_ready


def test_two_simultaneous_incident_closes_are_terminalization_pending() -> None:
    sui = execution()
    eth = execution(
        "c0eb0cd0-9d0f-4a2d-ae70-63c97dd37455",
        symbol="ETHUSDT",
    )
    decision = classify_status_fallback(evidence(sui, eth))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING
    assert decision.request_reconciliation


def test_close_evidence_arriving_during_bound_stays_pending() -> None:
    monitor = CertificationMonitorHealth(terminalization_timeout_seconds=120)
    pending = execution(exact_close=False, pending=True)
    assert record(monitor, evidence(pending)).state == (
        CertificationMonitorState.TERMINALIZATION_PENDING
    )
    exact = execution(exact_close=True, pending=False)
    decision = record(monitor, evidence(exact), NOW + timedelta(seconds=19))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING
    assert not decision.escalate


def test_remote_flat_without_exact_or_pending_evidence_is_ambiguous() -> None:
    item = execution(
        exact_close=False,
        error="entry order and fill attribution disagree",
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.SAFETY_AMBIGUOUS
    assert not decision.escalate


def test_partial_close_evidence_is_ambiguous() -> None:
    decision = classify_status_fallback(
        evidence(execution(exact_close=False, partial=True))
    )
    assert decision.state == CertificationMonitorState.SAFETY_AMBIGUOUS


def test_conflicting_close_identity_fails_fast() -> None:
    decision = classify_status_fallback(
        evidence(execution(exact_close=False, conflict=True))
    )
    assert decision.state == CertificationMonitorState.FAIL_FAST


def test_exact_owned_residual_orders_remain_terminalization_pending() -> None:
    decision = classify_status_fallback(evidence(execution(residuals=2)))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING


def test_unrelated_external_order_fails_immediately() -> None:
    decision = classify_status_fallback(
        evidence(execution(), unrelated_orders=1)
    )
    assert decision.state == CertificationMonitorState.FAIL_FAST


def test_authoritative_rest_evidence_overrides_stale_cached_counts() -> None:
    # Cached /v2/status counts are intentionally absent from the classifier.
    decision = classify_status_fallback(evidence(execution()))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING


def test_supervisor_does_not_terminate_reconciler_while_pending() -> None:
    monitor = CertificationMonitorHealth()
    decision = record(monitor, evidence(execution()))
    assert not decision.escalate
    assert decision.keep_reconciler_alive
    assert not decision.shutdown_ready


def test_repeated_pending_polls_are_one_deduplicated_incident() -> None:
    monitor = CertificationMonitorHealth()
    fallback = evidence(execution())
    record(monitor, fallback)
    record(monitor, fallback, NOW + timedelta(seconds=5))
    record(monitor, fallback, NOW + timedelta(seconds=10))
    assert monitor.incident_count == 1
    assert monitor.terminalization_pending_count == 1
    assert [event["event"] for event in monitor.events].count(
        "MONITOR_TERMINALIZATION_PENDING"
    ) == 1


def test_terminalization_bound_gets_one_ambiguity_window_before_fail_fast() -> None:
    monitor = CertificationMonitorHealth(terminalization_timeout_seconds=10)
    fallback = evidence(execution())
    record(monitor, fallback)
    boundary = record(monitor, fallback, NOW + timedelta(seconds=10))
    assert boundary.state == CertificationMonitorState.SAFETY_AMBIGUOUS
    assert not boundary.escalate
    final = record(monitor, fallback, NOW + timedelta(seconds=20))
    assert final.state == CertificationMonitorState.FAIL_FAST
    assert final.escalate


def test_status_recovery_returns_to_healthy_without_phase_mutation() -> None:
    monitor = CertificationMonitorHealth()
    record(monitor, evidence(execution()))
    decision = monitor.record_status_success(now=NOW + timedelta(seconds=20))
    assert decision.state == CertificationMonitorState.HEALTHY
    assert monitor.recovered_count == 1


def test_resolved_durable_terminalization_window_is_observed_once() -> None:
    monitor = CertificationMonitorHealth()

    monitor.record_resolved_terminalization_evidence(
        now=NOW,
        execution_id="execution-1",
        started_at="2026-07-29T11:59:58+00:00",
        completed_at="2026-07-29T11:59:59+00:00",
    )
    monitor.record_resolved_terminalization_evidence(
        now=NOW,
        execution_id="execution-1",
        started_at="2026-07-29T11:59:58+00:00",
        completed_at="2026-07-29T11:59:59+00:00",
    )

    assert monitor.terminalization_pending_count == 1
    events = [
        item for item in monitor.events
        if item["event"] == "MONITOR_TERMINALIZATION_PENDING"
    ]
    assert len(events) == 1
    assert events[0]["resolved"] is True
    assert events[0]["observed_via"] == "durable_terminalization_timestamps"


def test_process_persistence_kill_switch_and_unrelated_state_fail_fast() -> None:
    cases = (
        evidence(runner=False),
        evidence(uvicorn=False),
        evidence(listener=False),
        evidence(persistence=False),
        evidence(kill_switch=True),
        evidence(unrelated_positions=1),
        evidence(conflicts=1),
    )
    for fallback in cases:
        assert classify_status_fallback(fallback).state == (
            CertificationMonitorState.FAIL_FAST
        )


def test_monitor_has_no_exchange_mutation_surface() -> None:
    forbidden = {
        "create_order",
        "cancel_order",
        "set_trading_stop",
        "set_leverage",
        "close_position",
    }
    assert forbidden.isdisjoint(dir(CertificationMonitorHealth()))


def test_two_close_canary_requires_explicit_demo_order_authorization() -> None:
    source = (
        Path(__file__).parents[1]
        / "scripts"
        / "demo_v2_two_close_canary.py"
    ).read_text(encoding="utf-8")
    assert 'parser.add_argument("--allow-demo-orders", action="store_true")' in source
    assert '"BYBIT_LIVE_TRADING_ENABLED": "false"' in source
    assert '"BYBIT_ENABLE_TRADING": "false"' in source
    assert '"BYBIT_PRIVATE_DEMO_BASE_URL": "https://api-demo.bybit.com"' in source
    assert "if not args.allow_demo_orders:" in source
