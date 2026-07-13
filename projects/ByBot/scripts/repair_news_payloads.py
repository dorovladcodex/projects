from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sqlalchemy.orm import Session


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.db.news_repair import inspect_or_repair_news_rows  # noqa: E402
from app.db.persistence import PersistenceRepository  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit, repair, and quarantine historical NewsItem payloads."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report without writes")
    mode.add_argument("--apply", action="store_true", help="apply one transaction")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    repository = PersistenceRepository(settings.database_url, create_schema=False)
    if not repository.available:
        print("ERROR: database unavailable (credentials and URL omitted)", file=sys.stderr)
        return 1

    with Session(repository.engine) as session:
        if args.apply:
            with session.begin():
                report = inspect_or_repair_news_rows(session, apply=True)
        else:
            report = inspect_or_repair_news_rows(session, apply=False)
            session.rollback()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"mode: {mode}")
    print(f"total rows scanned: {report.total_rows_scanned}")
    print(f"valid rows: {report.valid_rows}")
    print(f"repairable rows: {report.repairable_rows}")
    print(f"quarantined rows: {report.quarantined_rows}")
    print("affected row IDs: " + (", ".join(report.affected_row_ids) or "none"))
    print("repaired row IDs: " + (", ".join(report.repaired_row_ids) or "none"))
    print("quarantined row IDs: " + (", ".join(report.quarantined_row_ids) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
