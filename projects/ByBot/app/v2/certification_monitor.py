from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class CertificationMonitorState(StrEnum):
    HEALTHY = "HEALTHY"
    STATUS_DEGRADED = "STATUS_DEGRADED"
    EXACT_ENTRY_ATTRIBUTION_PENDING = "EXACT_ENTRY_ATTRIBUTION_PENDING"
    PROTECTION_PENDING = "PROTECTION_PENDING"
    PROTECTED_POSITION_DEGRADED = "PROTECTED_POSITION_DEGRADED"
    TERMINALIZATION_PENDING = "TERMINALIZATION_PENDING"
    SAFETY_AMBIGUOUS = "SAFETY_AMBIGUOUS"
    FAIL_FAST = "FAIL_FAST"


class ProtectionEstablishmentState(StrEnum):
    EXACT_ENTRY_ATTRIBUTION_PENDING = "EXACT_ENTRY_ATTRIBUTION_PENDING"
    ENTRY_ACKNOWLEDGED = "ENTRY_ACKNOWLEDGED"
    ENTRY_PARTIALLY_FILLED = "ENTRY_PARTIALLY_FILLED"
    PROTECTION_PENDING = "PROTECTION_PENDING"
    PROTECTED = "PROTECTED"
    PROTECTION_INVALIDATED_BY_MARKET = "PROTECTION_INVALIDATED_BY_MARKET"
    UNPROTECTED_CONFIRMED = "UNPROTECTED_CONFIRMED"
    TERMINALIZATION_PENDING = "TERMINALIZATION_PENDING"
    SAFETY_AMBIGUOUS = "SAFETY_AMBIGUOUS"


@dataclass(frozen=True)
class ExecutionFallbackEvidence:
    execution_id: str
    symbol: str
    durable_state: str
    remote_position_open: bool
    protection_confirmed: bool
    remote_flat: bool
    exact_close_evidence: bool = False
    close_evidence_pending: bool = False
    exact_owned_residual_orders: int = 0
    partial_close: bool = False
    ownership_conflict: bool = False
    evidence_error: str | None = None
    entry_owned: bool = False
    protection_state: ProtectionEstablishmentState | None = None
    fill_at: str | None = None
    protection_started_at: str | None = None
    protection_requested_at: str | None = None
    protection_rest_confirmed_at: str | None = None
    protection_elapsed_ms: float | None = None
    protection_remaining_deadline_ms: float | None = None
    protection_attachment_started: bool = False
    authoritative_position_size: str | None = None
    authoritative_take_profit: str | None = None
    authoritative_stop_loss: str | None = None
    protection_order_ids: tuple[str, ...] = ()
    invalid_protection_reason: str | None = None
    entry_attribution_source: str | None = None
    entry_attribution_lookup_attempts: int = 0
    realtime_order_id: str | None = None
    realtime_order_link_id: str | None = None
    realtime_order_status: str | None = None
    realtime_order_quantity: str | None = None
    realtime_cumulative_quantity: str | None = None
    realtime_order_created_at: str | None = None
    realtime_order_updated_at: str | None = None
    realtime_identity_match: bool | None = None
    first_exact_attribution_at: str | None = None
    fill_history_observed_at: str | None = None


@dataclass(frozen=True)
class StatusFallbackEvidence:
    runner_alive: bool
    uvicorn_alive: bool
    port_listening: bool
    persistence_ok: bool
    kill_switch_active: bool = False
    executions: tuple[ExecutionFallbackEvidence, ...] = ()
    unrelated_positions: int = 0
    unrelated_orders: int = 0
    ownership_conflicts: int = 0
    authoritative_check_complete: bool = True


@dataclass(frozen=True)
class MonitorDecision:
    state: CertificationMonitorState
    escalate: bool
    reason: str | None = None
    request_reconciliation: bool = False
    keep_reconciler_alive: bool = True
    shutdown_ready: bool = False


def classify_status_fallback(
    evidence: StatusFallbackEvidence,
) -> MonitorDecision:
    """Classify one bounded, authoritative status fallback observation."""

    if not evidence.runner_alive:
        return _fail_fast("runner process exited unexpectedly")
    if not evidence.uvicorn_alive:
        return _fail_fast("Uvicorn process exited unexpectedly")
    if not evidence.port_listening:
        return _fail_fast("Uvicorn listener disappeared")
    if not evidence.persistence_ok:
        return _fail_fast("persistence fallback is unavailable")
    if evidence.kill_switch_active:
        return _fail_fast("kill switch active")
    if evidence.unrelated_positions:
        return _fail_fast("confirmed unrelated position")
    if evidence.unrelated_orders:
        return _fail_fast("confirmed unrelated order")
    if evidence.ownership_conflicts:
        return _fail_fast("exchange ownership conflict")

    for execution in evidence.executions:
        if execution.ownership_conflict:
            return _fail_fast(
                f"exchange ownership conflict execution={execution.execution_id}"
            )
        if (
            execution.remote_position_open
            and execution.protection_state
            == ProtectionEstablishmentState.PROTECTION_INVALIDATED_BY_MARKET
        ):
            return _fail_fast(
                f"authoritative TP/SL is invalid or contradictory "
                f"execution={execution.execution_id}: "
                f"{execution.invalid_protection_reason or 'unknown reason'}"
            )
        if (
            execution.remote_position_open
            and execution.protection_state
            == ProtectionEstablishmentState.UNPROTECTED_CONFIRMED
        ):
            return _fail_fast(
                f"open position is not authoritatively protected "
                f"execution={execution.execution_id}"
            )
        if (
            execution.remote_position_open
            and execution.protection_state
            == ProtectionEstablishmentState.SAFETY_AMBIGUOUS
        ):
            return MonitorDecision(
                CertificationMonitorState.SAFETY_AMBIGUOUS,
                False,
                execution.evidence_error
                or (
                    "exact entry/protection evidence is still being acquired "
                    f"execution={execution.execution_id}"
                ),
                request_reconciliation=True,
                keep_reconciler_alive=True,
            )
        if (
            execution.remote_position_open
            and execution.protection_state
            == ProtectionEstablishmentState.EXACT_ENTRY_ATTRIBUTION_PENDING
        ):
            if (
                execution.protection_remaining_deadline_ms is None
                or execution.protection_remaining_deadline_ms <= 0
            ):
                return MonitorDecision(
                    CertificationMonitorState.SAFETY_AMBIGUOUS,
                    False,
                    (
                        "exact entry attribution was not established inside "
                        f"the bounded window execution={execution.execution_id}"
                    ),
                    request_reconciliation=True,
                    keep_reconciler_alive=True,
                )
            return MonitorDecision(
                CertificationMonitorState.EXACT_ENTRY_ATTRIBUTION_PENDING,
                False,
                (
                    "exact realtime entry attribution is still propagating "
                    f"execution={execution.execution_id}"
                ),
                request_reconciliation=True,
                keep_reconciler_alive=True,
            )
        if (
            execution.remote_position_open
            and not execution.protection_confirmed
            and execution.protection_state not in {
                ProtectionEstablishmentState.ENTRY_ACKNOWLEDGED,
                ProtectionEstablishmentState.ENTRY_PARTIALLY_FILLED,
                ProtectionEstablishmentState.PROTECTION_PENDING,
            }
        ):
            return _fail_fast(
                f"open position protection state is not safely attributable "
                f"execution={execution.execution_id}"
            )
        if execution.partial_close:
            return MonitorDecision(
                CertificationMonitorState.SAFETY_AMBIGUOUS,
                False,
                f"partial close evidence execution={execution.execution_id}",
                request_reconciliation=True,
                keep_reconciler_alive=True,
            )

    protection_pending = [
        item for item in evidence.executions
        if not item.remote_flat
        and not item.protection_confirmed
        and item.entry_owned
        and item.protection_state in {
            ProtectionEstablishmentState.ENTRY_ACKNOWLEDGED,
            ProtectionEstablishmentState.ENTRY_PARTIALLY_FILLED,
            ProtectionEstablishmentState.PROTECTION_PENDING,
        }
    ]
    if protection_pending:
        expired = [
            item for item in protection_pending
            if item.protection_remaining_deadline_ms is None
            or item.protection_remaining_deadline_ms <= 0
        ]
        if expired:
            return _fail_fast(
                "bounded protection-establishment deadline expired "
                + ", ".join(
                    f"execution={item.execution_id}"
                    for item in expired
                )
            )
        return MonitorDecision(
            CertificationMonitorState.PROTECTION_PENDING,
            False,
            "exact owned entry is inside bounded protection-establishment window",
            request_reconciliation=True,
            keep_reconciler_alive=True,
        )

    flat_nonterminal = [
        item for item in evidence.executions if item.remote_flat
    ]
    if flat_nonterminal:
        contradictory = [
            item for item in flat_nonterminal
            if item.evidence_error and not item.close_evidence_pending
        ]
        if contradictory:
            return MonitorDecision(
                CertificationMonitorState.SAFETY_AMBIGUOUS,
                False,
                "; ".join(
                    f"execution={item.execution_id}: {item.evidence_error}"
                    for item in contradictory
                ),
                request_reconciliation=True,
                keep_reconciler_alive=True,
            )
        return MonitorDecision(
            CertificationMonitorState.TERMINALIZATION_PENDING,
            False,
            "remote flat while durable terminalization is pending",
            request_reconciliation=True,
            keep_reconciler_alive=True,
        )

    protected = [
        item for item in evidence.executions
        if item.remote_position_open
        and (
            item.protection_confirmed
            or item.protection_state == ProtectionEstablishmentState.PROTECTED
        )
    ]
    if protected and len(protected) == len(evidence.executions):
        return MonitorDecision(
            CertificationMonitorState.PROTECTED_POSITION_DEGRADED,
            False,
            "status unavailable; all owned positions remain protected",
            keep_reconciler_alive=True,
        )

    if not evidence.authoritative_check_complete and evidence.executions:
        return MonitorDecision(
            CertificationMonitorState.SAFETY_AMBIGUOUS,
            False,
            "authoritative fallback evidence is incomplete",
            request_reconciliation=True,
            keep_reconciler_alive=True,
        )

    return MonitorDecision(
        CertificationMonitorState.STATUS_DEGRADED,
        False,
        "status unavailable with no durable or remote exposure",
        keep_reconciler_alive=True,
    )


@dataclass
class CertificationMonitorHealth:
    """Bounded supervisor state which never kills useful reconciliation."""

    hard_timeout_seconds: float = 90.0
    terminalization_timeout_seconds: float = 120.0
    protection_timeout_seconds: float = 30.0
    state: CertificationMonitorState = CertificationMonitorState.HEALTHY
    degraded_since: datetime | None = None
    state_since: datetime | None = None
    attempts: int = 0
    incident_count: int = 0
    recovered_count: int = 0
    terminalization_pending_count: int = 0
    entry_attribution_pending_count: int = 0
    protection_pending_count: int = 0
    terminalization_bound_exceeded_at: datetime | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    observed_terminalization_ids: set[str] = field(default_factory=set)

    def record_resolved_terminalization_evidence(
        self,
        *,
        now: datetime,
        execution_id: str,
        started_at: str,
        completed_at: str,
    ) -> None:
        """Record a short pending window reconstructed after status recovery."""
        if execution_id in self.observed_terminalization_ids:
            return
        self.observed_terminalization_ids.add(execution_id)
        self.terminalization_pending_count += 1
        self.events.append({
            "event": "MONITOR_TERMINALIZATION_PENDING",
            "occurred_at": _aware(now).isoformat(),
            "execution_ids": [execution_id],
            "observed_via": "durable_terminalization_timestamps",
            "terminalization_started_at": started_at,
            "terminalization_completed_at": completed_at,
            "resolved": True,
        })

    def record_status_failure(
        self,
        *,
        now: datetime,
        evidence: StatusFallbackEvidence | None = None,
        runner_alive: bool | None = None,
        uvicorn_alive: bool | None = None,
        port_listening: bool | None = None,
        persistence_ok: bool | None = None,
        authoritative_account_safe: bool | None = None,
        error: str,
    ) -> MonitorDecision:
        now = _aware(now)
        if evidence is None:
            # Compatibility for callers/tests predating structured evidence.
            evidence = StatusFallbackEvidence(
                runner_alive=bool(runner_alive),
                uvicorn_alive=bool(uvicorn_alive),
                port_listening=bool(port_listening),
                persistence_ok=bool(persistence_ok),
                authoritative_check_complete=(
                    authoritative_account_safe is not None
                ),
                executions=(
                    ()
                    if authoritative_account_safe is not False
                    else (
                        ExecutionFallbackEvidence(
                            execution_id="unknown",
                            symbol="UNKNOWN",
                            durable_state="UNKNOWN",
                            remote_position_open=True,
                            protection_confirmed=False,
                            remote_flat=False,
                        ),
                    )
                ),
            )
        decision = classify_status_fallback(evidence)
        if decision.state == CertificationMonitorState.FAIL_FAST:
            return self._record_fail_fast(now, decision.reason, error)
        if (
            decision.state == CertificationMonitorState.TERMINALIZATION_PENDING
            and self.terminalization_bound_exceeded_at is not None
        ):
            decision = MonitorDecision(
                CertificationMonitorState.SAFETY_AMBIGUOUS,
                False,
                "terminalization remains incomplete after its bounded window",
                request_reconciliation=True,
                keep_reconciler_alive=True,
            )

        if self.degraded_since is None:
            self.degraded_since = now
            self.incident_count += 1
        self.attempts += 1
        if self.state != decision.state:
            self.state = decision.state
            self.state_since = now
            if decision.state == CertificationMonitorState.TERMINALIZATION_PENDING:
                self.terminalization_pending_count += 1
            if (
                decision.state
                == CertificationMonitorState.EXACT_ENTRY_ATTRIBUTION_PENDING
            ):
                self.entry_attribution_pending_count += 1
            if decision.state == CertificationMonitorState.PROTECTION_PENDING:
                self.protection_pending_count += 1
            self.events.append({
                "event": f"MONITOR_{decision.state.value}",
                "occurred_at": now.isoformat(),
                "reason": decision.reason,
                "error": _sanitize(error),
                "execution_ids": [
                    item.execution_id for item in evidence.executions
                ],
                "execution_evidence": [
                    _execution_evidence_summary(item)
                    for item in evidence.executions
                ],
            })

        state_elapsed = (
            (now - self.state_since).total_seconds()
            if self.state_since is not None else 0.0
        )
        if (
            decision.state == CertificationMonitorState.STATUS_DEGRADED
            and state_elapsed >= self.hard_timeout_seconds
        ):
            return self._record_fail_fast(
                now,
                "status API remained unavailable beyond bounded hard timeout",
                error,
            )
        if (
            decision.state == CertificationMonitorState.SAFETY_AMBIGUOUS
            and state_elapsed >= self.terminalization_timeout_seconds
        ):
            return self._record_fail_fast(
                now,
                "authoritative ambiguity exceeded bounded reconciliation window",
                error,
            )
        if (
            decision.state == CertificationMonitorState.TERMINALIZATION_PENDING
            and state_elapsed >= self.terminalization_timeout_seconds
        ):
            # Do not kill the only reconciler at the boundary. First move to
            # ambiguity and allow one further bounded evidence/reconcile pass.
            self.state = CertificationMonitorState.SAFETY_AMBIGUOUS
            self.state_since = now
            self.terminalization_bound_exceeded_at = now
            self.events.append({
                "event": "MONITOR_TERMINALIZATION_BOUND_EXCEEDED",
                "occurred_at": now.isoformat(),
                "duration_seconds": state_elapsed,
                "attempts": self.attempts,
                "execution_ids": [
                    item.execution_id for item in evidence.executions
                ],
            })
            return MonitorDecision(
                self.state,
                False,
                "terminalization evidence remains ambiguous after bounded wait",
                request_reconciliation=True,
                keep_reconciler_alive=True,
            )
        return decision

    def record_status_success(self, *, now: datetime) -> MonitorDecision:
        now = _aware(now)
        if self.state != CertificationMonitorState.HEALTHY:
            duration = (
                (now - self.degraded_since).total_seconds()
                if self.degraded_since is not None else 0.0
            )
            self.events.append({
                "event": "MONITOR_STATUS_RECOVERED",
                "occurred_at": now.isoformat(),
                "duration_seconds": duration,
                "attempts": self.attempts,
                "previous_state": self.state.value,
            })
            self.recovered_count += 1
        self.state = CertificationMonitorState.HEALTHY
        self.degraded_since = None
        self.state_since = None
        self.terminalization_bound_exceeded_at = None
        self.attempts = 0
        return MonitorDecision(self.state, False)

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = _aware(now or datetime.now(timezone.utc))
        return {
            "state": self.state.value,
            "degraded_since": (
                self.degraded_since.isoformat() if self.degraded_since else None
            ),
            "degraded_duration_seconds": (
                (current - self.degraded_since).total_seconds()
                if self.degraded_since else 0.0
            ),
            "state_since": self.state_since.isoformat() if self.state_since else None,
            "attempts": self.attempts,
            "incident_count": self.incident_count,
            "recovered_count": self.recovered_count,
            "terminalization_pending_count": self.terminalization_pending_count,
            "entry_attribution_pending_count": (
                self.entry_attribution_pending_count
            ),
            "protection_pending_count": self.protection_pending_count,
            "terminalization_bound_exceeded_at": (
                self.terminalization_bound_exceeded_at.isoformat()
                if self.terminalization_bound_exceeded_at else None
            ),
            "hard_timeout_seconds": self.hard_timeout_seconds,
            "terminalization_timeout_seconds": self.terminalization_timeout_seconds,
            "protection_timeout_seconds": self.protection_timeout_seconds,
        }

    def _record_fail_fast(
        self, now: datetime, reason: str | None, error: str,
    ) -> MonitorDecision:
        self.state = CertificationMonitorState.FAIL_FAST
        self.state_since = now
        self.events.append({
            "event": "MONITOR_FAIL_FAST",
            "occurred_at": now.isoformat(),
            "reason": reason,
            "error": _sanitize(error),
        })
        return MonitorDecision(
            self.state,
            True,
            reason,
            keep_reconciler_alive=False,
            shutdown_ready=True,
        )


def _fail_fast(reason: str) -> MonitorDecision:
    return MonitorDecision(
        CertificationMonitorState.FAIL_FAST,
        True,
        reason,
        keep_reconciler_alive=False,
        shutdown_ready=True,
    )


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None else value.astimezone(timezone.utc)
    )


def _sanitize(value: str) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:240]


def _execution_evidence_summary(
    item: ExecutionFallbackEvidence,
) -> dict[str, Any]:
    return {
        "execution_id": item.execution_id,
        "symbol": item.symbol,
        "durable_state": item.durable_state,
        "protection_state": (
            item.protection_state.value if item.protection_state else None
        ),
        "entry_owned": item.entry_owned,
        "fill_at": item.fill_at,
        "protection_started_at": item.protection_started_at,
        "protection_requested_at": item.protection_requested_at,
        "protection_rest_confirmed_at": item.protection_rest_confirmed_at,
        "elapsed_ms": item.protection_elapsed_ms,
        "remaining_deadline_ms": item.protection_remaining_deadline_ms,
        "authoritative_position_size": item.authoritative_position_size,
        "take_profit": item.authoritative_take_profit,
        "stop_loss": item.authoritative_stop_loss,
        "protection_order_ids": list(item.protection_order_ids),
        "entry_attribution_source": item.entry_attribution_source,
        "entry_attribution_lookup_attempts": (
            item.entry_attribution_lookup_attempts
        ),
        "realtime_order_id": item.realtime_order_id,
        "realtime_order_link_id": item.realtime_order_link_id,
        "realtime_order_status": item.realtime_order_status,
        "realtime_order_quantity": item.realtime_order_quantity,
        "realtime_cumulative_quantity": item.realtime_cumulative_quantity,
        "realtime_order_created_at": item.realtime_order_created_at,
        "realtime_order_updated_at": item.realtime_order_updated_at,
        "realtime_identity_match": item.realtime_identity_match,
        "first_exact_attribution_at": item.first_exact_attribution_at,
        "fill_history_observed_at": item.fill_history_observed_at,
    }
