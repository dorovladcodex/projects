from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import Symbol
from app.v2.outcomes import (
    OptionalManagementGateEvidence,
    RuntimeOutcome,
    SafeDegradedOutcome,
    classify_optional_management_gate,
)
from tests.test_v2_runtime_observability import runtime


FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "v2_runtime_outcome_replays.json"
    ).read_text(encoding="utf-8")
)
CYCLE_743 = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "v2_cycle_743_optional_management_ownership.json"
    ).read_text(encoding="utf-8")
)


def test_replay_corpus_contains_all_recent_incident_classes() -> None:
    assert {row["name"] for row in FIXTURE} == {
        "fill_to_protection_propagation_race",
        "exact_realtime_order_attribution_race",
        "execution_history_propagation",
        "terminal_residual_order_propagation",
        "stale_ticker_paired_with_trades_timestamp",
        "safe_stale_management_deferral",
        "optional_management_update_ownership_unconfirmed",
        "simultaneous_close_terminalization",
        "status_timeout_during_terminalization",
        "postgresql_startup_readiness",
        "dependency_degradation_and_recovery",
    }
    assert all(
        row["classification"]
        in {
            "SAFE_DEGRADED",
            "OBSERVABILITY_WARNING",
            "SUCCESS",
        }
        for row in FIXTURE
    )
    assert not any(row["mutation_allowed"] for row in FIXTURE)


def test_cycle_743_is_reclassified_without_weakening_safety() -> None:
    assert CYCLE_743["cycle_id"].endswith(":743")
    assert CYCLE_743["expected_classification"] == "SAFE_DEGRADED"
    assert CYCLE_743["expected_cycle_failure_increment"] == 0
    assert CYCLE_743["ambiguous_exchange_mutation_submitted"] is False


def test_certification_mode_is_explicit_and_validated() -> None:
    assert Settings(_env_file=None).v2_certification_mode == "STRICT"
    assert (
        Settings(
            _env_file=None, v2_certification_mode="discovery"
        ).v2_certification_mode
        == "DISCOVERY"
    )
    with pytest.raises(ValidationError, match="DISCOVERY or STRICT"):
        Settings(_env_file=None, v2_certification_mode="permissive")


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        (
            ("REST_POSITION", "REST_PROTECTION", "WS_FILL", "CACHED_STATUS"),
            RuntimeOutcome.SUCCESS,
        ),
        (
            ("WS_FILL", "REST_POSITION", "REST_PROTECTION", "CACHED_STATUS"),
            RuntimeOutcome.SUCCESS,
        ),
        (
            ("REST_POSITION", "ORDER_HISTORY", "FILL_HISTORY", "REST_PROTECTION"),
            RuntimeOutcome.SUCCESS,
        ),
        (
            ("FILL_HISTORY", "REST_POSITION", "ORDER_HISTORY", "REST_PROTECTION"),
            RuntimeOutcome.SUCCESS,
        ),
        (
            ("REST_PROTECTION", "CACHED_STATUS", "REST_POSITION"),
            RuntimeOutcome.SUCCESS,
        ),
        (
            ("TERMINAL_CLOSE", "RESIDUAL_CANCELLATION"),
            RuntimeOutcome.SAFE_DEGRADED,
        ),
        (
            ("DEPENDENCY_TIMEOUT", "REST_POSITION", "REST_PROTECTION"),
            RuntimeOutcome.SAFE_DEGRADED,
        ),
    ],
)
def test_event_ordering_permutations_preserve_safe_mutation_contract(
    events: tuple[str, ...], expected: RuntimeOutcome
) -> None:
    remote_open = "TERMINAL_CLOSE" not in events
    degraded = (
        "DEPENDENCY_TIMEOUT" in events
        or "TERMINAL_CLOSE" in events
    )
    details = classify_optional_management_gate(
        OptionalManagementGateEvidence(
            remote_position_open=remote_open,
            position_ownership_confirmed=True,
            current_protection_confirmed=True,
            replace_targets_confirmed=not degraded,
            input_fresh="DEPENDENCY_TIMEOUT" not in events,
        )
    )
    assert details.classification == expected
    assert details.exchange_mutation_attempted is False


def test_proposed_update_ownership_failure_is_safe_degraded() -> None:
    details = classify_optional_management_gate(
        OptionalManagementGateEvidence(
            remote_position_open=True,
            position_ownership_confirmed=True,
            current_protection_confirmed=True,
            replace_targets_confirmed=False,
            input_fresh=True,
        )
    )
    assert details.classification == RuntimeOutcome.SAFE_DEGRADED
    assert (
        details.code
        == "MANAGEMENT_UPDATE_SKIPPED_OWNERSHIP_UNCONFIRMED"
    )


@pytest.mark.parametrize(
    "evidence",
    [
        OptionalManagementGateEvidence(
            remote_position_open=True,
            position_ownership_confirmed=False,
            current_protection_confirmed=True,
            replace_targets_confirmed=False,
            input_fresh=True,
        ),
        OptionalManagementGateEvidence(
            remote_position_open=True,
            position_ownership_confirmed=True,
            current_protection_confirmed=False,
            replace_targets_confirmed=False,
            input_fresh=True,
        ),
        OptionalManagementGateEvidence(
            remote_position_open=True,
            position_ownership_confirmed=True,
            current_protection_confirmed=True,
            replace_targets_confirmed=True,
            input_fresh=True,
            ownership_conflict=True,
        ),
    ],
)
def test_current_position_or_protection_ambiguity_remains_critical(
    evidence: OptionalManagementGateEvidence,
) -> None:
    assert (
        classify_optional_management_gate(evidence).classification
        == RuntimeOutcome.SAFETY_CRITICAL
    )


def test_safe_degradation_does_not_increment_cycle_failures(
    tmp_path: Path,
) -> None:
    app, repository, _ = runtime(tmp_path, (Symbol.BTCUSDT,))
    app._record_failure(
        SafeDegradedOutcome(
            "optional update skipped",
            code="MANAGEMENT_UPDATE_SKIPPED_OWNERSHIP_UNCONFIRMED",
            evidence={"exchange_mutation_attempted": False},
        ),
        stage="position_monitoring",
        cycle_id="runtime-test:1",
        symbol=Symbol.BTCUSDT,
    )

    assert app.failure_occurrences == {}
    app._refresh_status_snapshot()
    assert app.status()["total_cycle_failures"] == 0
    assert app.status()["safe_degraded_events"] == 1
    incident = next(iter(repository.incidents.values()))
    assert incident.event_type == "V2_SAFE_DEGRADED"
    assert incident.payload["exchange_mutation_attempted"] is False


def test_safe_degradation_is_deduplicated_and_runtime_continues(
    tmp_path: Path,
) -> None:
    app, repository, _ = runtime(tmp_path, (Symbol.BTCUSDT,))
    for cycle in (1, 2):
        app._record_failure(
            SafeDegradedOutcome(
                "optional update skipped",
                code="MANAGEMENT_UPDATE_SKIPPED_OWNERSHIP_UNCONFIRMED",
            ),
            stage="position_monitoring",
            cycle_id=f"runtime-test:{cycle}",
            symbol=Symbol.BTCUSDT,
        )

    asyncio.run(app.cycle())
    assert len(repository.incidents) == 1
    assert next(iter(repository.incidents.values())).payload[
        "occurrence_count"
    ] == 2
    assert app.cycles == 1
    assert app.stop_new_entries is False
