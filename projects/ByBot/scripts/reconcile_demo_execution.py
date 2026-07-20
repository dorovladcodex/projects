from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import DemoDiagnosticsConfig, RESOLVED_STATES  # noqa: E402
from app.bybit.demo_execution_recovery import (  # noqa: E402
    apply_demo_execution_repair,
    diagnose_demo_execution,
    diagnosis_payload,
    exact_close_reconciliation_blockers,
)
from app.db.persistence import PersistenceRepository  # noqa: E402


def _maintenance_payload(
    diagnosis: Any,
    repository: PersistenceRepository,
    *,
    mode: str,
) -> dict[str, Any]:
    payload = diagnosis_payload(diagnosis)
    blockers = exact_close_reconciliation_blockers(diagnosis)
    kill_switch = repository.load_demo_kill_switch() or {
        "active": False,
        "reasons": [],
        "events": [],
    }
    unresolved = [
        str(item.id)
        for item in repository.load_demo_executions()
        if item.state not in RESOLVED_STATES
    ]
    linked_events = [
        {
            "id": event.get("id"),
            "event_type": event.get("event_type"),
            "active": bool(event.get("active")),
        }
        for event in kill_switch.get("events") or []
        if str(event.get("execution_id") or "") == str(diagnosis.record.id)
    ]
    payload.update({
        "mode": mode,
        "current_durable_state": diagnosis.record.state.value,
        "proposed_terminal_state": (
            diagnosis.proposed_state.value if diagnosis.proposed_state else None
        ),
        "can_reconcile": not blockers,
        "blockers": blockers,
        "active_incident_relationship": {
            "kill_switch_active": bool(kill_switch.get("active")),
            "execution_is_unresolved": str(diagnosis.record.id) in unresolved,
            "linked_kill_switch_events": linked_events,
        },
        "exchange_mutations_performed": False,
    })
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evidence-based, GET-only Bybit Demo execution reconciliation"
    )
    parser.add_argument("--execution-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    try:
        config = DemoDiagnosticsConfig.load(env_path=Path(".env"))
        repository = PersistenceRepository(config.database_url, create_schema=False)
        if not repository.available:
            raise RuntimeError("PostgreSQL persistence is unavailable")
        diagnosis = diagnose_demo_execution(
            config, args.execution_id, repository=repository
        )
        before = _maintenance_payload(
            diagnosis, repository, mode="apply" if args.apply else "dry-run"
        )
        if args.dry_run:
            print(json.dumps(before, indent=2, default=str))
            return 0 if before["can_reconcile"] else 1

        if not before["can_reconcile"]:
            before["apply_performed"] = False
            print(json.dumps(before, indent=2, default=str))
            return 1
        applied = apply_demo_execution_repair(diagnosis, repository)
        if not applied:
            before["apply_performed"] = False
            before["apply_error"] = "durable reconciliation transaction failed"
            print(json.dumps(before, indent=2, default=str))
            return 1
        verified = diagnose_demo_execution(
            config, args.execution_id, repository=repository
        )
        after = _maintenance_payload(verified, repository, mode="apply-verification")
        after["apply_performed"] = True
        after["previous_durable_state"] = before["current_durable_state"]
        print(json.dumps(after, indent=2, default=str))
        return 0 if verified.record.state in RESOLVED_STATES else 1
    except Exception as exc:
        print(json.dumps({
            "status": "FAIL",
            "error": type(exc).__name__,
            "exchange_mutations_performed": False,
        }, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
