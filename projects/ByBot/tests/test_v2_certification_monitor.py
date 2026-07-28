from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.v2.certification_monitor import (
    CertificationMonitorHealth,
    CertificationMonitorState,
)


NOW = datetime(2026, 7, 28, 13, 53, tzinfo=timezone.utc)


def failure(
    monitor: CertificationMonitorHealth,
    *,
    now: datetime = NOW,
    runner: bool = True,
    uvicorn: bool = True,
    port: bool = True,
    persistence: bool = True,
    account_safe: bool | None = True,
):
    return monitor.record_status_failure(
        now=now,
        runner_alive=runner,
        uvicorn_alive=uvicorn,
        port_listening=port,
        persistence_ok=persistence,
        authoritative_account_safe=account_safe,
        error="TimeoutError: timed out",
    )


def test_one_status_timeout_does_not_terminate_monitoring() -> None:
    monitor = CertificationMonitorHealth(hard_timeout_seconds=90)
    decision = failure(monitor)
    assert decision.escalate is False
    assert decision.state == CertificationMonitorState.STATUS_DEGRADED


def test_multiple_timeouts_form_one_degraded_incident() -> None:
    monitor = CertificationMonitorHealth(hard_timeout_seconds=90)
    failure(monitor)
    failure(monitor, now=NOW + timedelta(seconds=10))
    failure(monitor, now=NOW + timedelta(seconds=20))
    assert monitor.incident_count == 1
    assert monitor.attempts == 3
    assert [
        item["event"] for item in monitor.events
    ].count("MONITOR_STATUS_DEGRADED") == 1


def test_status_recovery_resumes_healthy_monitoring() -> None:
    monitor = CertificationMonitorHealth()
    failure(monitor)
    decision = monitor.record_status_success(
        now=NOW + timedelta(seconds=12)
    )
    assert decision.state == CertificationMonitorState.HEALTHY
    assert monitor.recovered_count == 1
    assert monitor.attempts == 0
    recovered = [
        item for item in monitor.events
        if item["event"] == "MONITOR_STATUS_RECOVERED"
    ]
    assert recovered[0]["duration_seconds"] == 12


def test_dead_uvicorn_escalates_immediately() -> None:
    monitor = CertificationMonitorHealth()
    decision = failure(monitor, uvicorn=False)
    assert decision.escalate is True
    assert "Uvicorn" in str(decision.reason)


def test_dead_runner_escalates_immediately() -> None:
    monitor = CertificationMonitorHealth()
    decision = failure(monitor, runner=False)
    assert decision.escalate is True
    assert "runner" in str(decision.reason)


def test_port_alive_status_unavailable_remains_bounded() -> None:
    monitor = CertificationMonitorHealth(hard_timeout_seconds=30)
    assert failure(monitor).escalate is False
    assert failure(
        monitor, now=NOW + timedelta(seconds=29)
    ).escalate is False
    decision = failure(monitor, now=NOW + timedelta(seconds=30))
    assert decision.escalate is True
    assert decision.state == CertificationMonitorState.FAILED


def test_authoritative_account_fallback_allows_transient_timeout() -> None:
    monitor = CertificationMonitorHealth()
    decision = failure(monitor, account_safe=True)
    assert decision.escalate is False
    assert monitor.state == CertificationMonitorState.STATUS_DEGRADED


def test_authoritative_account_fallback_failure_escalates() -> None:
    monitor = CertificationMonitorHealth()
    decision = failure(monitor, account_safe=False)
    assert decision.escalate is True
    assert "exchange safety" in str(decision.reason)


def test_hard_timeout_escalates_once() -> None:
    monitor = CertificationMonitorHealth(hard_timeout_seconds=5)
    failure(monitor)
    decision = failure(monitor, now=NOW + timedelta(seconds=5))
    assert decision.escalate is True
    assert [
        item["event"] for item in monitor.events
    ].count("MONITOR_STATUS_HARD_TIMEOUT") == 1


def test_monitor_state_machine_has_no_exchange_mutation_surface() -> None:
    monitor = CertificationMonitorHealth()
    forbidden = {
        "create_order",
        "cancel_order",
        "set_trading_stop",
        "set_leverage",
        "close_position",
    }
    assert forbidden.isdisjoint(dir(monitor))


def test_compact_monitoring_can_continue_after_recovery() -> None:
    monitor = CertificationMonitorHealth()
    failure(monitor)
    monitor.record_status_success(now=NOW + timedelta(seconds=1))
    assert monitor.record_status_success(
        now=NOW + timedelta(seconds=2)
    ).state == CertificationMonitorState.HEALTHY
    assert monitor.snapshot(now=NOW + timedelta(seconds=2))["incident_count"] == 1
