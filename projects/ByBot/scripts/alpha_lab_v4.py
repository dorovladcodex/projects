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
from app.v4.alpha_lab import run_alpha_lab_v4  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the deterministic read-only ByBot V4 opportunity research lab."
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "artifacts" / "alpha-lab-v4",
    )
    parser.add_argument("--cadence-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.cadence_seconds < 5:
        raise SystemExit("cadence must be at least 5 seconds")
    config = DemoDiagnosticsConfig.load(env_path=ROOT / ".env")
    repository = PersistenceRepository(config.database_url, create_schema=False)
    # Research uses code defaults only; mutation-capable local environment
    # values must not leak into an offline backfill.
    settings = Settings(_env_file=None)
    result = run_alpha_lab_v4(
        repository=repository,
        settings=settings,
        output_dir=args.output_dir,
        cadence_seconds=args.cadence_seconds,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
