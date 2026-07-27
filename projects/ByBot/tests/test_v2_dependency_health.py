from __future__ import annotations

from datetime import datetime, timedelta, timezone
import socket
from urllib.error import URLError

from app.v2.dependency_health import (
    DependencyHealthState,
    ExternalDependencyHealth,
    is_transient_dependency_error,
)


class Repo:
    def __init__(self):
        self.incidents = {}

    def save_v2_incident(self, incident):
        self.incidents[str(incident.id)] = incident
        return True


def manager(*, hard=300):
    return ExternalDependencyHealth(
        run_id="run",
        repository=Repo(),
        initial_backoff_seconds=1,
        maximum_backoff_seconds=8,
        hard_outage_seconds=hard,
        jitter=lambda low, high: 0,
    )


def dns_error():
    return URLError(socket.gaierror(11001, "getaddrinfo failed"))


def test_dns_failure_without_position_pauses_entries_without_hard_failure() -> None:
    health = manager()
    decision = health.record_failure(
        dns_error(), dependency="bybit_rest", host="api.bybit.com",
        active_position_count=0, protection_confirmed=True,
    )
    assert decision.handled and not decision.hard_failure
    assert health.entries_paused
    assert health.state == DependencyHealthState.DEGRADED


def test_repeated_dns_errors_update_one_durable_incident() -> None:
    health = manager()
    now = datetime.now(timezone.utc)
    for offset in (0, 2, 5):
        health.record_failure(
            dns_error(), dependency="bybit_rest", host="api-demo.bybit.com",
            active_position_count=1, protection_confirmed=True,
            now=now + timedelta(seconds=offset),
        )
    assert len(health.repository.incidents) == 1
    assert health.retry_count == 3
    assert health.current_backoff_seconds == 4


def test_protected_position_survives_bounded_rest_degradation() -> None:
    health = manager()
    decision = health.record_failure(
        dns_error(), dependency="bybit_rest", host="api-demo.bybit.com",
        active_position_count=1, protection_confirmed=True,
    )
    assert not decision.hard_failure
    incident = next(iter(health.repository.incidents.values()))
    assert incident.payload["protection_confirmed"] is True
    assert incident.payload["entries_paused"] is True


def test_unconfirmed_protection_escalates_immediately() -> None:
    health = manager()
    decision = health.record_failure(
        dns_error(), dependency="bybit_rest", host="api-demo.bybit.com",
        active_position_count=1, protection_confirmed=False,
    )
    assert decision.hard_failure


def test_hard_outage_bound_is_finite() -> None:
    health = manager(hard=30)
    now = datetime.now(timezone.utc)
    health.record_failure(
        dns_error(), dependency="bybit_rest", host="api.bybit.com",
        active_position_count=0, protection_confirmed=True, now=now,
    )
    decision = health.record_failure(
        dns_error(), dependency="bybit_rest", host="api.bybit.com",
        active_position_count=0, protection_confirmed=True,
        now=now + timedelta(seconds=31),
    )
    assert decision.hard_failure


def test_recovery_requires_authoritative_success() -> None:
    health = manager()
    health.record_failure(
        dns_error(), dependency="bybit_rest", host="api.bybit.com",
        active_position_count=0, protection_confirmed=True,
    )
    health.begin_recovery()
    assert not health.record_recovered(
        dependency="bybit_rest", active_position_count=0,
        protection_confirmed=True,
        authoritative_reconciliation_succeeded=False,
    )
    assert health.entries_paused
    assert health.record_recovered(
        dependency="bybit_rest", active_position_count=0,
        protection_confirmed=True,
        authoritative_reconciliation_succeeded=True,
    )
    assert health.state == DependencyHealthState.HEALTHY
    assert not health.entries_paused


def test_restored_degraded_state_does_not_resume_entries() -> None:
    first = manager()
    first.record_failure(
        dns_error(), dependency="bybit_rest", host="api.bybit.com",
        active_position_count=0, protection_confirmed=True,
    )
    restored = ExternalDependencyHealth(
        run_id="run", repository=Repo(),
        initial_backoff_seconds=1, maximum_backoff_seconds=8,
        hard_outage_seconds=300, jitter=lambda low, high: 0,
        restored=first.snapshot(),
    )
    assert restored.state == DependencyHealthState.DEGRADED
    assert restored.entries_paused


def test_transport_and_timeout_are_transient_but_programming_error_is_not() -> None:
    assert is_transient_dependency_error(ConnectionError("down"))
    assert is_transient_dependency_error(TimeoutError("slow"))
    assert not is_transient_dependency_error(ValueError("bad decimal"))
