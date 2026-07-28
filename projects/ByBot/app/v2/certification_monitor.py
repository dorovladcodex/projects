from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class CertificationMonitorState(StrEnum):
    HEALTHY = "HEALTHY"
    STATUS_DEGRADED = "STATUS_DEGRADED"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"


@dataclass(frozen=True)
class MonitorDecision:
    state: CertificationMonitorState
    escalate: bool
    reason: str | None = None


@dataclass
class CertificationMonitorHealth:
    """Bounded, observational health state for frozen/development monitors."""

    hard_timeout_seconds: float = 90.0
    state: CertificationMonitorState = CertificationMonitorState.HEALTHY
    degraded_since: datetime | None = None
    attempts: int = 0
    incident_count: int = 0
    recovered_count: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def record_status_failure(
        self,
        *,
        now: datetime,
        runner_alive: bool,
        uvicorn_alive: bool,
        port_listening: bool,
        persistence_ok: bool,
        authoritative_account_safe: bool | None,
        error: str,
    ) -> MonitorDecision:
        now = _aware(now)
        immediate = None
        if not runner_alive:
            immediate = "runner process exited unexpectedly"
        elif not uvicorn_alive:
            immediate = "Uvicorn process exited unexpectedly"
        elif not port_listening:
            immediate = "Uvicorn listener disappeared"
        elif not persistence_ok:
            immediate = "persistence fallback is unavailable"
        elif authoritative_account_safe is False:
            immediate = "authoritative exchange safety could not be established"
        if immediate:
            self.state = CertificationMonitorState.FAILED
            self.events.append({
                "event": "MONITOR_HARD_FAILURE",
                "occurred_at": now.isoformat(),
                "reason": immediate,
                "error": _sanitize(error),
            })
            return MonitorDecision(self.state, True, immediate)

        if self.state == CertificationMonitorState.HEALTHY:
            self.state = CertificationMonitorState.STATUS_DEGRADED
            self.degraded_since = now
            self.attempts = 0
            self.incident_count += 1
            self.events.append({
                "event": "MONITOR_STATUS_DEGRADED",
                "occurred_at": now.isoformat(),
                "error": _sanitize(error),
            })
        self.attempts += 1
        elapsed = (
            (now - self.degraded_since).total_seconds()
            if self.degraded_since is not None else 0.0
        )
        if elapsed >= self.hard_timeout_seconds:
            self.state = CertificationMonitorState.FAILED
            reason = (
                "status API remained unavailable beyond bounded hard timeout"
            )
            self.events.append({
                "event": "MONITOR_STATUS_HARD_TIMEOUT",
                "occurred_at": now.isoformat(),
                "duration_seconds": elapsed,
                "attempts": self.attempts,
                "reason": reason,
            })
            return MonitorDecision(self.state, True, reason)
        return MonitorDecision(self.state, False)

    def record_status_success(self, *, now: datetime) -> MonitorDecision:
        now = _aware(now)
        if self.state in {
            CertificationMonitorState.STATUS_DEGRADED,
            CertificationMonitorState.RECOVERING,
        }:
            self.state = CertificationMonitorState.RECOVERING
            duration = (
                (now - self.degraded_since).total_seconds()
                if self.degraded_since is not None else 0.0
            )
            self.events.append({
                "event": "MONITOR_STATUS_RECOVERED",
                "occurred_at": now.isoformat(),
                "duration_seconds": duration,
                "attempts": self.attempts,
            })
            self.recovered_count += 1
            self.state = CertificationMonitorState.HEALTHY
            self.degraded_since = None
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
            "attempts": self.attempts,
            "incident_count": self.incident_count,
            "recovered_count": self.recovered_count,
            "hard_timeout_seconds": self.hard_timeout_seconds,
        }


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None else value.astimezone(timezone.utc)
    )


def _sanitize(value: str) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text[:240]
