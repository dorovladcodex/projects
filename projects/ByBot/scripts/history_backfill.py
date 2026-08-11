"""Backfill Bybit historical market data into the research database.

Read-only against the exchange: only public market endpoints are called and no
credentials are loaded. Writes go exclusively to the research database given by
--database-url or DATABASE_URL, which must not be the production `bybot`
database.

Example:

    python scripts/history_backfill.py --symbols BTCUSDT ETHUSDT \
        --from 2023-01-01 --to 2026-08-01 --series kline --interval 1
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.history.client import BybitHistoryClient  # noqa: E402
from app.history.ingest import HistoryBackfill, SeriesReport  # noqa: E402
from app.history.models import KlineInterval, OpenInterestInterval  # noqa: E402
from app.history.storage import HistoryStorage, psycopg_dsn  # noqa: E402

DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT",
    "AVAXUSDT", "LINKUSDT", "LTCUSDT", "NEARUSDT", "SUIUSDT", "WIFUSDT",
)

PRODUCTION_DATABASE_NAMES = frozenset({"bybot"})


def parse_day(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def guard_research_database(dsn: str) -> str:
    """Refuse to write into the production database."""
    normalized = psycopg_dsn(dsn)
    name = normalized.rsplit("/", 1)[-1].split("?", 1)[0]
    if name in PRODUCTION_DATABASE_NAMES:
        raise SystemExit(
            f"refusing to write history into production database {name!r}; "
            "point --database-url at the research database"
        )
    return normalized


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--from", dest="start", required=True, help="UTC day, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", required=True, help="UTC day, YYYY-MM-DD")
    parser.add_argument(
        "--series", nargs="+", default=["kline"],
        choices=["kline", "funding", "open_interest"],
    )
    parser.add_argument(
        "--interval", default=KlineInterval.ONE_MINUTE.value,
        choices=[member.value for member in KlineInterval],
    )
    parser.add_argument(
        "--oi-interval", default=OpenInterestInterval.FIVE_MINUTES.value,
        choices=[member.value for member in OpenInterestInterval],
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--base-url", default="https://api.bybit.com")
    parser.add_argument(
        "--request-interval", type=float, default=0.06,
        help="minimum seconds between public requests",
    )
    parser.add_argument("--report-only", action="store_true", help="print coverage and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.database_url:
        raise SystemExit("--database-url or DATABASE_URL is required")
    dsn = guard_research_database(args.database_url)

    # Validate the range before opening a connection so a bad invocation fails
    # on its arguments rather than on a database error.
    start_ms, end_ms = parse_day(args.start), parse_day(args.end)
    if start_ms >= end_ms and not args.report_only:
        raise SystemExit("--from must be earlier than --to")

    storage = HistoryStorage(dsn)
    storage.create_schema()

    if args.report_only:
        rows = storage.all_coverage()
        if not rows:
            print("no backfill recorded yet")
            return 0
        for row in rows:
            span_days = (row.covered_to_ms - row.covered_from_ms) / 86_400_000
            print(
                f"{row.series:14s} {row.symbol:10s} {row.interval:10s} "
                f"{span_days:8.1f}d rows={row.row_count:,}"
            )
        return 0

    client = BybitHistoryClient(
        args.base_url, min_request_interval_seconds=args.request_interval
    )

    def emit(report: SeriesReport) -> None:
        print(f"  {report.summary()}", flush=True)

    backfill = HistoryBackfill(client, storage, progress=emit)
    kline_interval = KlineInterval(args.interval)
    oi_interval = OpenInterestInterval(args.oi_interval)

    total_inserted = 0
    for symbol in args.symbols:
        print(f"{symbol}", flush=True)
        if "kline" in args.series:
            total_inserted += backfill.klines(symbol, kline_interval, start_ms, end_ms).inserted
        if "funding" in args.series:
            total_inserted += backfill.funding(symbol, start_ms, end_ms).inserted
        if "open_interest" in args.series:
            total_inserted += backfill.open_interest(
                symbol, oi_interval, start_ms, end_ms
            ).inserted

    print(f"\ninserted {total_inserted:,} new rows; {client.request_count:,} public requests")

    if "kline" in args.series:
        print("\ncoverage:")
        for symbol in args.symbols:
            gap = backfill.gap_report(symbol, kline_interval)
            print(
                f"  {symbol:10s} stored={gap.stored_rows:>9,} expected={gap.expected_rows:>9,} "
                f"missing={gap.missing_rows:>7,} ({gap.coverage_pct:5.2f}%)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
