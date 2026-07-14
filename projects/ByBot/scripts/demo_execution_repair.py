from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bybit.demo_diagnostics import DemoDiagnosticsConfig  # noqa: E402
from app.bybit.demo_execution_recovery import (  # noqa: E402
    apply_demo_execution_repair,
    diagnose_demo_execution,
)
from app.db.persistence import PersistenceRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--confirm-finalize", action="store_true")
    args = parser.parse_args()
    try:
        config = DemoDiagnosticsConfig.load()
        repository = PersistenceRepository(config.database_url, create_schema=False)
        diagnosis = diagnose_demo_execution(
            config, args.execution_id, repository=repository
        )
        if not diagnosis.repairable:
            print("EXECUTION REPAIR: REFUSED", file=sys.stderr)
            for blocker in diagnosis.blockers:
                print(f"- {blocker}", file=sys.stderr)
            return 1
        print("REMOTE DEMO STATE FLAT: PASS")
        print("KNOWN ORDERS TERMINAL: PASS")
        print("AUTHORITATIVE FILL HISTORY: PASS")
        print(f"PROPOSED TERMINAL STATE: {diagnosis.proposed_state.value}")
        if not args.confirm_finalize:
            print("EXECUTION REPAIR: DRY RUN")
            return 0
        if not apply_demo_execution_repair(diagnosis, repository):
            print("EXECUTION REPAIR: FAIL", file=sys.stderr)
            return 1
        print("EXECUTION REPAIR: PASS")
        return 0
    except Exception as exc:
        print(f"EXECUTION REPAIR: FAIL\nERROR: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
