from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import DemoDiagnosticsConfig  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db.persistence import PersistenceRepository  # noqa: E402
from app.v5.alpha_lab import run_alpha_lab_v5  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the deterministic read-only ByBot V5 Alpha Lab."
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "artifacts" / "alpha-lab-v5",
    )
    parser.add_argument(
        "--v4-artifact-dir", type=Path,
        default=ROOT / "artifacts" / "alpha-lab-v4",
    )
    args = parser.parse_args()
    config = DemoDiagnosticsConfig.load(env_path=ROOT / ".env")
    repository = PersistenceRepository(config.database_url, create_schema=False)
    # Code defaults prevent local mutation settings and unverified account fees
    # from leaking into the offline analysis.
    settings = Settings(_env_file=None)
    result = run_alpha_lab_v5(
        repository=repository,
        settings=settings,
        output_dir=args.output_dir,
        v4_artifact_dir=args.v4_artifact_dir,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
