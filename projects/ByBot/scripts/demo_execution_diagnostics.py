from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bybit.demo_diagnostics import DemoDiagnosticsConfig  # noqa: E402
from app.bybit.demo_execution_recovery import (  # noqa: E402
    diagnose_demo_execution,
    diagnosis_payload,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-id", required=True)
    args = parser.parse_args()
    try:
        result = diagnose_demo_execution(
            DemoDiagnosticsConfig.load(), args.execution_id
        )
        print(json.dumps(diagnosis_payload(result), indent=2))
        return 0 if not result.blockers else 1
    except Exception as exc:
        print(f"READ-ONLY EXECUTION DIAGNOSTICS: FAIL\nERROR: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
