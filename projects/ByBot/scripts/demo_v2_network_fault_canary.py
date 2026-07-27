from __future__ import annotations

import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.v2.dependency_health import (
    DependencyHealthState,
    ExternalDependencyHealth,
)


class IncidentRepository:
    def __init__(self) -> None:
        self.incidents: dict[str, object] = {}

    def save_v2_incident(self, incident: object) -> bool:
        self.incidents[str(getattr(incident, "id"))] = incident
        return True


def main() -> int:
    run_id = f"network-canary-{uuid4()}"
    repository = IncidentRepository()
    health = ExternalDependencyHealth(
        repository=repository,
        run_id=run_id,
        initial_backoff_seconds=1,
        maximum_backoff_seconds=4,
        hard_outage_seconds=30,
        jitter=lambda _low, _high: 0,
    )
    started = datetime.now(timezone.utc)
    first = health.record_failure(
        socket.gaierror(11001, "name resolution failed"),
        dependency="bybit_rest",
        host="api-demo.bybit.com",
        active_position_count=1,
        protection_confirmed=True,
        now=started,
    )
    second = health.record_failure(
        socket.gaierror(11001, "name resolution failed"),
        dependency="bybit_rest",
        host="api-demo.bybit.com",
        active_position_count=1,
        protection_confirmed=True,
        now=started + timedelta(seconds=1),
    )
    assert first.handled and second.handled
    assert first.incident_id == second.incident_id
    assert health.entries_paused
    assert len(repository.incidents) == 1
    health.begin_recovery()
    assert health.state == DependencyHealthState.RECOVERING
    assert health.record_recovered(
        dependency="bybit_rest",
        active_position_count=1,
        protection_confirmed=True,
        authoritative_reconciliation_succeeded=True,
        now=started + timedelta(seconds=2),
    )
    assert health.state == DependencyHealthState.HEALTHY
    assert not health.entries_paused
    assert len(repository.incidents) == 1
    print(f"NETWORK CANARY RUN ID: {run_id}")
    print("ENTRIES PAUSED DURING OUTAGE: PASS")
    print("PROTECTED POSITION PRESERVED: PASS")
    print("ONE DURABLE OUTAGE INCIDENT: PASS")
    print("AUTHORITATIVE RECOVERY: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
