from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import random
import socket
from typing import Any, Callable
from urllib.error import URLError
from uuid import NAMESPACE_URL, uuid5

from app.v2.models import V2Incident


class DependencyHealthState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"


class ExternalDependencySafetyError(RuntimeError):
    """A bounded outage can no longer be proven safe."""


@dataclass(frozen=True)
class DependencyFailureDecision:
    handled: bool
    hard_failure: bool
    retry_at: datetime | None
    incident_id: str | None


def is_transient_dependency_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(
            current,
            (
                socket.gaierror,
                URLError,
                TimeoutError,
                ConnectionError,
                OSError,
            ),
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


class ExternalDependencyHealth:
    """Run-scoped, durable and bounded Bybit connectivity state."""

    def __init__(
        self,
        *,
        run_id: str,
        repository: Any,
        initial_backoff_seconds: float,
        maximum_backoff_seconds: float,
        hard_outage_seconds: float,
        jitter: Callable[[float, float], float] | None = None,
        restored: dict[str, Any] | None = None,
    ) -> None:
        restored = dict(restored or {})
        self.run_id = run_id
        self.repository = repository
        self.initial_backoff_seconds = max(0.1, initial_backoff_seconds)
        self.maximum_backoff_seconds = max(
            self.initial_backoff_seconds, maximum_backoff_seconds
        )
        self.hard_outage_seconds = max(
            self.maximum_backoff_seconds, hard_outage_seconds
        )
        self._jitter = jitter or random.uniform
        self.state = DependencyHealthState(
            restored.get("state", DependencyHealthState.HEALTHY.value)
        )
        self.outage_started_at = _optional_datetime(
            restored.get("outage_started_at")
        )
        self.outage_ended_at = _optional_datetime(restored.get("outage_ended_at"))
        self.next_retry_at = _optional_datetime(restored.get("next_retry_at"))
        self.retry_count = int(restored.get("retry_count") or 0)
        self.current_backoff_seconds = float(
            restored.get("current_backoff_seconds") or 0
        )
        self.affected_hosts: set[str] = set(restored.get("affected_hosts") or [])
        self.error_categories: set[str] = set(
            restored.get("error_categories") or []
        )
        self.entries_paused = bool(restored.get("entries_paused", False))
        self.authoritative_reconciliation_succeeded = bool(
            restored.get("authoritative_reconciliation_succeeded", True)
        )
        self.last_error: str | None = restored.get("last_error")
        self.last_recovery_at = _optional_datetime(restored.get("last_recovery_at"))
        self.incident_id: str | None = restored.get("incident_id")

    def should_attempt(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return self.next_retry_at is None or current >= self.next_retry_at

    def record_failure(
        self,
        exc: BaseException,
        *,
        dependency: str,
        host: str,
        active_position_count: int,
        protection_confirmed: bool,
        now: datetime | None = None,
    ) -> DependencyFailureDecision:
        if not is_transient_dependency_error(exc):
            return DependencyFailureDecision(False, False, None, None)
        current = now or datetime.now(timezone.utc)
        if (
            self.state == DependencyHealthState.HEALTHY
            or self.outage_started_at is None
        ):
            self.outage_started_at = current
            self.outage_ended_at = None
            self.retry_count = 0
            self.affected_hosts.clear()
            self.error_categories.clear()
            self.incident_id = str(
                uuid5(
                    NAMESPACE_URL,
                    (
                        f"bybot-v2-dependency-outage:{self.run_id}:"
                        f"{dependency}:{current.isoformat()}"
                    ),
                )
            )
        self.state = DependencyHealthState.DEGRADED
        self.entries_paused = True
        self.authoritative_reconciliation_succeeded = False
        self.retry_count += 1
        raw_backoff = min(
            self.maximum_backoff_seconds,
            self.initial_backoff_seconds * (2 ** max(0, self.retry_count - 1)),
        )
        jitter = self._jitter(0, min(raw_backoff * 0.25, 2.0))
        self.current_backoff_seconds = raw_backoff + max(0.0, jitter)
        self.next_retry_at = current + timedelta(
            seconds=self.current_backoff_seconds
        )
        self.affected_hosts.add(host)
        self.error_categories.add(_error_category(exc))
        self.last_error = _safe_message(exc)
        duration = (current - self.outage_started_at).total_seconds()
        hard_failure = duration >= self.hard_outage_seconds or (
            active_position_count > 0 and not protection_confirmed
        )
        self._persist(
            dependency=dependency,
            active_position_count=active_position_count,
            protection_confirmed=protection_confirmed,
            recovery_result=(
                "HARD_BOUND_EXCEEDED"
                if duration >= self.hard_outage_seconds
                else "PROTECTION_UNCONFIRMED"
                if active_position_count > 0 and not protection_confirmed
                else "RETRY_SCHEDULED"
            ),
            now=current,
        )
        return DependencyFailureDecision(
            True, hard_failure, self.next_retry_at, self.incident_id
        )

    def begin_recovery(self) -> None:
        if self.state != DependencyHealthState.HEALTHY:
            self.state = DependencyHealthState.RECOVERING

    def record_recovered(
        self,
        *,
        dependency: str,
        active_position_count: int,
        protection_confirmed: bool,
        authoritative_reconciliation_succeeded: bool,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(timezone.utc)
        if not authoritative_reconciliation_succeeded:
            return False
        self.state = DependencyHealthState.HEALTHY
        self.entries_paused = False
        self.authoritative_reconciliation_succeeded = True
        self.outage_ended_at = current
        self.last_recovery_at = current
        self.next_retry_at = None
        self.current_backoff_seconds = 0
        self.last_error = None
        self._persist(
            dependency=dependency,
            active_position_count=active_position_count,
            protection_confirmed=protection_confirmed,
            recovery_result="AUTHORITATIVE_RECONCILIATION_SUCCEEDED",
            now=current,
        )
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "outage_started_at": (
                self.outage_started_at.isoformat()
                if self.outage_started_at else None
            ),
            "outage_ended_at": (
                self.outage_ended_at.isoformat() if self.outage_ended_at else None
            ),
            "next_retry_at": (
                self.next_retry_at.isoformat() if self.next_retry_at else None
            ),
            "retry_count": self.retry_count,
            "current_backoff_seconds": self.current_backoff_seconds,
            "affected_hosts": sorted(self.affected_hosts),
            "error_categories": sorted(self.error_categories),
            "entries_paused": self.entries_paused,
            "authoritative_reconciliation_succeeded": (
                self.authoritative_reconciliation_succeeded
            ),
            "last_error": self.last_error,
            "last_recovery_at": (
                self.last_recovery_at.isoformat() if self.last_recovery_at else None
            ),
            "incident_id": self.incident_id,
        }

    def _persist(
        self,
        *,
        dependency: str,
        active_position_count: int,
        protection_confirmed: bool,
        recovery_result: str,
        now: datetime,
    ) -> None:
        if not self.incident_id:
            return
        payload = {
            **self.snapshot(),
            "dependency": dependency,
            "host": sorted(self.affected_hosts),
            "error_category": sorted(self.error_categories),
            "outage_duration_seconds": (
                (now - self.outage_started_at).total_seconds()
                if self.outage_started_at else 0
            ),
            "active_position_count": active_position_count,
            "protection_confirmed": protection_confirmed,
            "recovery_result": recovery_result,
        }
        self.repository.save_v2_incident(
            V2Incident(
                id=self.incident_id,
                run_id=self.run_id,
                event_type="EXTERNAL_DEPENDENCY_OUTAGE",
                error_category="external_dependency",
                payload=payload,
                occurred_at=self.outage_started_at or now,
            )
        )


def _optional_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _error_category(exc: BaseException) -> str:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, socket.gaierror) or "getaddrinfo" in str(current):
            return "DNS_RESOLUTION"
        if isinstance(current, (TimeoutError,)):
            return "TIMEOUT"
        if isinstance(current, (ConnectionError, URLError, OSError)):
            return "TRANSPORT"
        current = current.__cause__ or current.__context__
    return type(exc).__name__.upper()


def _safe_message(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return message[:250] or type(exc).__name__
