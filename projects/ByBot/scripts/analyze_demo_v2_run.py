from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import DemoDiagnosticsConfig
from app.v2.run_analysis import analyze_demo_v2_run


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate read-only durable/remote analysis for one Demo V2 run."
    )
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    result = analyze_demo_v2_run(
        args.run_id,
        DemoDiagnosticsConfig.load(env_path=ROOT / ".env"),
        artifact_root=ROOT / "artifacts" / "demo-v2",
    )
    print(json.dumps({
        "run_id": result["run_id"],
        "analysis_result": result["analysis_result"],
        "completed_trades": result["completed_trades"],
        "unresolved_execution_ids": result["unresolved_execution_ids"],
        "analysis_markdown": result["analysis_markdown"],
        "mutation_attempted": False,
    }, indent=2))
    return 0 if result["analysis_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
