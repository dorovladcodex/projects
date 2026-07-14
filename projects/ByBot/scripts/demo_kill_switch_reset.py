from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bybit.demo_diagnostics import (  # noqa: E402
    DemoDiagnosticsConfig,
    DemoDiagnosticsError,
    evaluate_demo_recovery_readiness,
    format_demo_recovery_readiness,
    run_demo_diagnostics,
)
from app.db.persistence import PersistenceRepository  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed recovery of a verified Bybit Demo kill-switch latch"
    )
    parser.add_argument("--execution-id", required=True)
    parser.add_argument(
        "--confirm-reset", action="store_true",
        help="Apply the reset; omission performs a read-only dry run",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = DemoDiagnosticsConfig.load()
        repository = PersistenceRepository(config.database_url, create_schema=False)
        result = run_demo_diagnostics(config, repository=repository)
        readiness = evaluate_demo_recovery_readiness(result, args.execution_id)
        print(format_demo_recovery_readiness(readiness))
        if readiness.blockers:
            print("KILL SWITCH RESET: REFUSED", file=sys.stderr)
            for blocker in readiness.blockers:
                print(f"- {blocker}", file=sys.stderr)
            return 1
        print("REMOTE DEMO STATE FLAT: PASS")
        print("DURABLE EXECUTIONS RESOLVED: PASS")
        print("RECOVERABLE LATCH: PASS")
        if not args.confirm_reset:
            print("KILL SWITCH RESET: DRY RUN")
            return 0
        if readiness.linked_activation_id == "repair-audit-inference":
            if not repository.link_demo_kill_switch_execution(
                args.execution_id,
                reason=(
                    "guarded linkage from run/order timestamps and complete "
                    "sleep-resume execution repair audit"
                ),
            ):
                print("KILL SWITCH RESET: FAIL", file=sys.stderr)
                return 1
            result = run_demo_diagnostics(config, repository=repository)
            readiness = evaluate_demo_recovery_readiness(result, args.execution_id)
            if readiness.blockers or readiness.linked_activation_id in {
                None, "repair-audit-inference"
            }:
                print("KILL SWITCH RESET: FAIL", file=sys.stderr)
                return 1
        if not repository.reset_demo_kill_switch(
            args.execution_id,
            reason="operator-confirmed recovery after flat Demo reconciliation",
        ):
            print("KILL SWITCH RESET: FAIL", file=sys.stderr)
            return 1
        print("KILL SWITCH RESET: PASS")
        return 0
    except Exception as exc:
        error = exc if isinstance(exc, DemoDiagnosticsError) else type(exc).__name__
        print(f"KILL SWITCH RESET: FAIL\nERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
