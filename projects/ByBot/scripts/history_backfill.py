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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.history.client import BybitHistoryClient  # noqa: E402
from app.history.ingest import HistoryBackfill  # noqa: E402
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
        choices=["kline", "spot", "funding", "open_interest"],
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
    parser.add_argument(
        "--workers", type=int, default=1,
        help="symbols fetched concurrently; each worker holds its own client and connection",
    )
    return parser


PRINT_LOCK = Lock()


def backfill_symbol(symbol: str, args, dsn: str) -> tuple[str, int, int]:
    """Fetch one symbol end to end.

    Each worker builds its own client and storage: the request throttle is
    per-client state and psycopg connections are not shared across threads.
    """
    storage = HistoryStorage(dsn)

    def quarantine(sym: str, interval: str, category: str, row, reason: str) -> None:
        storage.quarantine_kline(
            symbol=sym, interval=interval, start_ms=int(row[0]),
            category=category, reason=reason, raw_row=row,
        )

    client = BybitHistoryClient(
        args.base_url,
        min_request_interval_seconds=args.request_interval,
        on_anomaly=quarantine,
    )
    backfill = HistoryBackfill(client, storage)
    start_ms, end_ms = parse_day(args.start), parse_day(args.end)
    kline_interval = KlineInterval(args.interval)

    inserted = 0
    reports = []
    if "kline" in args.series:
        reports.append(backfill.klines(symbol, kline_interval, start_ms, end_ms))
    if "spot" in args.series:
        reports.append(backfill.spot_klines(symbol, kline_interval, start_ms, end_ms))
    if "funding" in args.series:
        reports.append(backfill.funding(symbol, start_ms, end_ms))
    if "open_interest" in args.series:
        reports.append(
            backfill.open_interest(
                symbol, OpenInterestInterval(args.oi_interval), start_ms, end_ms
            )
        )

    inserted = sum(report.inserted for report in reports)
    with PRINT_LOCK:
        print(symbol, flush=True)
        for report in reports:
            print(f"  {report.summary()}", flush=True)
        if client.anomaly_count:
            print(f"  quarantined {client.anomaly_count} malformed bars", flush=True)
    return symbol, inserted, client.request_count


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

    kline_interval = KlineInterval(args.interval)
    workers = max(1, args.workers)
    total_inserted = total_requests = 0

    if workers == 1:
        for symbol in args.symbols:
            _, inserted, requests = backfill_symbol(symbol, args, dsn)
            total_inserted += inserted
            total_requests += requests
    else:
        print(f"fetching {len(args.symbols)} symbols with {workers} workers\n", flush=True)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(backfill_symbol, symbol, args, dsn) for symbol in args.symbols
            ]
            for future in as_completed(futures):
                _, inserted, requests = future.result()
                total_inserted += inserted
                total_requests += requests

    print(f"\ninserted {total_inserted:,} new rows; {total_requests:,} public requests")

    if "kline" in args.series:
        reporter = HistoryBackfill(
            BybitHistoryClient(args.base_url), HistoryStorage(dsn)
        )
        print("\ncoverage:")
        for symbol in args.symbols:
            gap = reporter.gap_report(symbol, kline_interval)
            print(
                f"  {symbol:10s} stored={gap.stored_rows:>9,} expected={gap.expected_rows:>9,} "
                f"missing={gap.missing_rows:>7,} ({gap.coverage_pct:5.2f}%)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
