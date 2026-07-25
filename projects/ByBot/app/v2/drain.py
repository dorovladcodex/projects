from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class V2RunPhase(str, Enum):
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    RECONCILING = "RECONCILING"
    FINISHED = "FINISHED"


_PHASE_ORDER = {
    V2RunPhase.RUNNING: 0,
    V2RunPhase.DRAINING: 1,
    V2RunPhase.RECONCILING: 2,
    V2RunPhase.FINISHED: 3,
}


@dataclass(frozen=True)
class V2DrainStatus:
    phase: V2RunPhase
    nominal_end_at: datetime | None
    drain_started_at: datetime | None
    drain_deadline_at: datetime | None
    seconds_until_drain: float | None
    seconds_until_nominal_end: float | None
    drain_seconds_remaining: float | None
    timed_out: bool
    active_execution_ids: tuple[str, ...]

    @property
    def entries_allowed(self) -> bool:
        return self.phase == V2RunPhase.RUNNING


class V2DrainController:
    """Pure UTC run-finalization state machine; it never mutates exchange state."""

    def __init__(
        self,
        nominal_end_at: datetime | None,
        *,
        lead_seconds: int,
        timeout_seconds: int,
        restored_phase: V2RunPhase | str | None = None,
        drain_started_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        self.nominal_end_at = _utc(nominal_end_at) if nominal_end_at else None
        self.lead_seconds = lead_seconds
        self.timeout_seconds = timeout_seconds
        self.phase = V2RunPhase(restored_phase or V2RunPhase.RUNNING)
        self.drain_started_at = (
            _utc(drain_started_at) if drain_started_at else None
        )
        current = _utc(now or datetime.now(timezone.utc))
        if (
            restored_phase is not None
            and self.phase == V2RunPhase.RUNNING
            and self.nominal_end_at is not None
            and current >= self.nominal_end_at
        ):
            self.phase = V2RunPhase.DRAINING
            self.drain_started_at = (
                self.drain_started_at
                or self.nominal_end_at - timedelta(seconds=self.lead_seconds)
            )

    def evaluate(
        self,
        *,
        now: datetime | None = None,
        active_execution_ids: list[str] | tuple[str, ...] = (),
        finalization_ready: bool | None = None,
    ) -> V2DrainStatus:
        current = _utc(now or datetime.now(timezone.utc))
        active = tuple(sorted(set(active_execution_ids)))
        ready = not active if finalization_ready is None else finalization_ready
        nominal = self.nominal_end_at
        if nominal is None:
            if self.phase != V2RunPhase.RUNNING and ready:
                self._advance(V2RunPhase.FINISHED)
            return self._status(current, active, timed_out=False)

        drain_at = nominal - timedelta(seconds=self.lead_seconds)
        deadline = nominal + timedelta(seconds=self.timeout_seconds)
        if current >= drain_at and self.phase == V2RunPhase.RUNNING:
            self._advance(V2RunPhase.DRAINING)
            self.drain_started_at = drain_at
        if current >= nominal and self.phase in {
            V2RunPhase.RUNNING,
            V2RunPhase.DRAINING,
        }:
            self._advance(V2RunPhase.RECONCILING)
            self.drain_started_at = self.drain_started_at or current
        if (
            self.phase == V2RunPhase.RECONCILING
            or current >= nominal
        ) and self.phase != V2RunPhase.RUNNING and ready:
            self._advance(V2RunPhase.FINISHED)

        timed_out = current >= deadline and bool(active)
        return self._status(current, active, timed_out=timed_out)

    def force_draining(self, *, now: datetime | None = None) -> V2DrainStatus:
        current = _utc(now or datetime.now(timezone.utc))
        if self.phase == V2RunPhase.RUNNING:
            self._advance(V2RunPhase.DRAINING)
            self.drain_started_at = current
        return self._status(current, (), timed_out=False)

    def advance(self, phase: V2RunPhase) -> None:
        self._advance(phase)

    def _advance(self, phase: V2RunPhase) -> None:
        if _PHASE_ORDER[phase] > _PHASE_ORDER[self.phase]:
            self.phase = phase

    def _status(
        self, current: datetime, active: tuple[str, ...], *, timed_out: bool
    ) -> V2DrainStatus:
        nominal = self.nominal_end_at
        drain_at = (
            nominal - timedelta(seconds=self.lead_seconds) if nominal else None
        )
        deadline = (
            nominal + timedelta(seconds=self.timeout_seconds) if nominal else None
        )
        return V2DrainStatus(
            phase=self.phase,
            nominal_end_at=nominal,
            drain_started_at=self.drain_started_at,
            drain_deadline_at=deadline,
            seconds_until_drain=(
                max(0.0, (drain_at - current).total_seconds())
                if drain_at and self.phase == V2RunPhase.RUNNING else 0.0
                if drain_at else None
            ),
            seconds_until_nominal_end=(
                max(0.0, (nominal - current).total_seconds()) if nominal else None
            ),
            drain_seconds_remaining=(
                max(0.0, (deadline - current).total_seconds())
                if deadline and self.phase != V2RunPhase.FINISHED else 0.0
                if deadline else None
            ),
            timed_out=timed_out,
            active_execution_ids=active,
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("V2 run deadline must be timezone-aware")
    return value.astimezone(timezone.utc)
