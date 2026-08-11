from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import DemoDiagnosticsConfig  # noqa: E402
from app.db.persistence import PersistenceRepository  # noqa: E402
from app.v2.alpha_lab import run_alpha_lab  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the deterministic read-only ByBot V3 Alpha Lab."
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=ROOT / "artifacts" / "demo-v2" / "all-time-alpha-baseline-20260803"
        / "consolidated-alpha-baseline.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "artifacts" / "alpha-lab-v3",
    )
    args = parser.parse_args()
    config = DemoDiagnosticsConfig.load(env_path=ROOT / ".env")
    repository = PersistenceRepository(config.database_url, create_schema=False)
    result = run_alpha_lab(
        baseline_path=args.baseline,
        output_dir=args.output_dir,
        repository=repository,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
