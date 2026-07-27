from __future__ import annotations

from datetime import datetime, timedelta, timezone
import socket
from urllib.error import URLError
import asyncio
import json
from pathlib import Path

from app.bybit.demo import DemoPreMutationDependencyError
from app.models import Symbol
from app.v2.dependency_health import (
    DependencyHealthState,
    ExternalDependencyHealth,
    is_transient_dependency_error,
)
from tests.test_v2_execution import _admitted_candidate, coordinator, features


REPLAY = (
    Path(__file__).parent
    / "fixtures"
    / "demo_replay"
    / "sol_cycle_1012_preflight_transport.json"
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


def test_sol_cycle_1012_pre_mutation_transport_is_typed_no_order_rejection() -> None:
    replay = json.loads(REPLAY.read_text(encoding="utf-8"))
    service, repository, demo = coordinator()
    candidate = _admitted_candidate(service, repository, Symbol.SOLUSDT)
    demo.failure = DemoPreMutationDependencyError(
        "Demo REST entry preflight is temporarily unavailable",
        stage="entry_read_preflight",
        error_category=replay["error_category"],
    )

    result = service.execute(candidate)

    assert result["rejection_code"] == replay["expected_rejection_code"]
    assert result["handled_external_dependency_rejection"] is True
    assert result["exchange_order_submission_invoked"] is False
    assert result["reservation_release_result"] == "RELEASED"
    assert repository.load_demo_executions() == []
    reservation = next(
        row for row in service.portfolio.reservations
        if str(row.id) == result["reservation_id"]
    )
    assert reservation.state.value == "RELEASED"
    assert service.portfolio.release(
        reservation.id, activate_cooldown=False
    ) is False


def test_runtime_dependency_rejection_pauses_entries_without_cycle_failure(
    tmp_path,
) -> None:
    from tests.test_v2_runtime_observability import runtime

    app, repository, _ = runtime(tmp_path, (Symbol.SOLUSDT,))
    candidate = app.strategies[1].evaluate(features(Symbol.SOLUSDT))
    candidate.run_id = app.run_id
    candidate.admitted = False
    candidate.state = "EXECUTION_REJECTED"
    result = {
        "handled_pre_submit_rejection": True,
        "handled_external_dependency_rejection": True,
        "rejection_code": "EXTERNAL_DEPENDENCY_UNAVAILABLE",
        "rejection_message": "Demo REST entry preflight is temporarily unavailable",
        "dependency_error_category": "TRANSPORT",
        "reservation_id": "reservation",
        "reservation_release_result": "RELEASED",
        "pre_submit_audit": {},
        "rejected_at": datetime.now(timezone.utc).isoformat(),
    }

    async def collect() -> None:
        future = asyncio.get_running_loop().create_future()
        future.set_result(result)
        await app._collect_dispatches(
            [(candidate, future)], f"{app.run_id}:1012"
        )

    asyncio.run(collect())

    assert sum(app.failure_occurrences.values()) == 0
    assert app.signal_metrics["pre_submit_rejections"] == 1
    assert app.entries_paused is True
    assert app.dependency_health.retry_count == 1
    assert len([
        item for item in repository.incidents.values()
        if item.event_type == "EXTERNAL_DEPENDENCY_OUTAGE"
    ]) == 1
