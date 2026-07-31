from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RuntimeOutcome(str, Enum):
    SAFETY_CRITICAL = "SAFETY_CRITICAL"
    DATA_INTEGRITY_CRITICAL = "DATA_INTEGRITY_CRITICAL"
    SAFE_DEGRADED = "SAFE_DEGRADED"
    EXPECTED_REJECTION = "EXPECTED_REJECTION"
    OBSERVABILITY_WARNING = "OBSERVABILITY_WARNING"
    SUCCESS = "SUCCESS"


NON_FAILURE_OUTCOMES = {
    RuntimeOutcome.SAFE_DEGRADED,
    RuntimeOutcome.EXPECTED_REJECTION,
    RuntimeOutcome.OBSERVABILITY_WARNING,
    RuntimeOutcome.SUCCESS,
}


@dataclass(frozen=True)
class RuntimeOutcomeDetails:
    classification: RuntimeOutcome
    code: str
    evidence: dict[str, Any] = field(default_factory=dict)
    exchange_mutation_attempted: bool = False


@dataclass(frozen=True)
class OptionalManagementGateEvidence:
    remote_position_open: bool
    position_ownership_confirmed: bool
    current_protection_confirmed: bool
    replace_targets_confirmed: bool
    input_fresh: bool
    unrelated_exchange_state: bool = False
    ownership_conflict: bool = False


def classify_optional_management_gate(
    evidence: OptionalManagementGateEvidence,
) -> RuntimeOutcomeDetails:
    if evidence.unrelated_exchange_state or evidence.ownership_conflict:
        return RuntimeOutcomeDetails(
            RuntimeOutcome.SAFETY_CRITICAL,
            "CURRENT_EXCHANGE_OWNERSHIP_CONFLICT",
        )
    if evidence.remote_position_open and not evidence.position_ownership_confirmed:
        return RuntimeOutcomeDetails(
            RuntimeOutcome.SAFETY_CRITICAL,
            "CURRENT_POSITION_OWNERSHIP_UNCONFIRMED",
        )
    if evidence.remote_position_open and not evidence.current_protection_confirmed:
        return RuntimeOutcomeDetails(
            RuntimeOutcome.SAFETY_CRITICAL,
            "CURRENT_PROTECTION_OWNERSHIP_UNCONFIRMED",
        )
    if not evidence.remote_position_open:
        return RuntimeOutcomeDetails(
            RuntimeOutcome.SAFE_DEGRADED,
            "MANAGEMENT_UPDATE_SKIPPED_REMOTE_FLAT",
        )
    if not evidence.input_fresh:
        return RuntimeOutcomeDetails(
            RuntimeOutcome.SAFE_DEGRADED,
            "MANAGEMENT_UPDATE_SKIPPED_STALE_INPUT",
        )
    if not evidence.replace_targets_confirmed:
        return RuntimeOutcomeDetails(
            RuntimeOutcome.SAFE_DEGRADED,
            "MANAGEMENT_UPDATE_SKIPPED_OWNERSHIP_UNCONFIRMED",
        )
    return RuntimeOutcomeDetails(
        RuntimeOutcome.SUCCESS,
        "MANAGEMENT_MUTATION_GATE_CONFIRMED",
    )


class RuntimeOutcomeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        classification: RuntimeOutcome,
        code: str,
        evidence: dict[str, Any] | None = None,
        exchange_mutation_attempted: bool = False,
    ) -> None:
        super().__init__(message)
        self.details = RuntimeOutcomeDetails(
            classification=classification,
            code=code,
            evidence=dict(evidence or {}),
            exchange_mutation_attempted=exchange_mutation_attempted,
        )


class SafetyCriticalOutcome(RuntimeOutcomeError):
    def __init__(
        self, message: str, *, code: str, evidence: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message,
            classification=RuntimeOutcome.SAFETY_CRITICAL,
            code=code,
            evidence=evidence,
        )


class DataIntegrityCriticalOutcome(RuntimeOutcomeError):
    def __init__(
        self, message: str, *, code: str, evidence: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message,
            classification=RuntimeOutcome.DATA_INTEGRITY_CRITICAL,
            code=code,
            evidence=evidence,
        )


class SafeDegradedOutcome(RuntimeOutcomeError):
    def __init__(
        self, message: str, *, code: str, evidence: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message,
            classification=RuntimeOutcome.SAFE_DEGRADED,
            code=code,
            evidence=evidence,
            exchange_mutation_attempted=False,
        )


class ExpectedRejectionOutcome(RuntimeOutcomeError):
    def __init__(
        self, message: str, *, code: str, evidence: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message,
            classification=RuntimeOutcome.EXPECTED_REJECTION,
            code=code,
            evidence=evidence,
            exchange_mutation_attempted=False,
        )


class ObservabilityWarningOutcome(RuntimeOutcomeError):
    def __init__(
        self, message: str, *, code: str, evidence: dict[str, Any] | None = None
    ) -> None:
        super().__init__(
            message,
            classification=RuntimeOutcome.OBSERVABILITY_WARNING,
            code=code,
            evidence=evidence,
            exchange_mutation_attempted=False,
        )


def typed_outcome(exc: BaseException) -> RuntimeOutcomeDetails | None:
    if isinstance(exc, RuntimeOutcomeError):
        return exc.details
    name = type(exc).__name__
    message = str(exc).lower()
    if name in {
        "DemoSafetyError",
        "DemoExchangeError",
        "ExternalDependencySafetyError",
    }:
        return RuntimeOutcomeDetails(
            classification=RuntimeOutcome.SAFETY_CRITICAL,
            code="DEMO_SAFETY_INVARIANT_FAILED",
        )
    if (
        name in {
            "PersistenceStartupError",
            "IntegrityError",
            "OperationalError",
            "DatabaseError",
        }
        or "invalid state transition" in message
        or "duplicate terminal" in message
        or "duplicate ledger" in message
        or "duplicate capacity" in message
        or "global orderid" in message
        or "global execid" in message
    ):
        return RuntimeOutcomeDetails(
            classification=RuntimeOutcome.DATA_INTEGRITY_CRITICAL,
            code="DURABLE_STATE_INTEGRITY_FAILED",
        )
    return None
