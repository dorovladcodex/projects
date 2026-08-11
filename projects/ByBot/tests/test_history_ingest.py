from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from app.history.client import BybitHistoryClient
from app.history.ingest import (
    SERIES_FUNDING,
    SERIES_KLINE,
    HistoryBackfill,
    SeriesReport,
)
from app.history.models import Kline, KlineInterval, OpenInterestInterval
from app.history.storage import WriteResult, psycopg_dsn

import scripts.history_backfill as cli

MINUTE_MS = 60_000
# Bars carry a realistic epoch: the model rejects 0 so a dropped timestamp
# cannot pass as a valid 1970 bar.
T0 = 1_700_000_000_000


def ok(rows: list[Any]) -> dict[str, Any]:
    return {"retCode": 0, "result": {"list": rows}}


def bar(offset_ms: int) -> list[str]:
    return [str(T0 + offset_ms), "100", "101", "99", "100", "1", "100"]


class FakeStorage:
    """In-memory stand-in with the same contract as HistoryStorage."""

    def __init__(self, *, duplicates: int = 0) -> None:
        self.klines: list[Kline] = []
        self.funding: list[Any] = []
        self.open_interest: list[Any] = []
        self.progress: list[dict[str, Any]] = []
        self.duplicates = duplicates
        self.sessions_opened = 0

    @contextmanager
    def session(self) -> Any:
        self.sessions_opened += 1
        yield self

    def _result(self, count: int) -> WriteResult:
        inserted = max(count - self.duplicates, 0)
        return WriteResult(received=count, inserted=inserted)

    def write_klines(self, bars: list[Kline]) -> WriteResult:
        self.klines.extend(bars)
        return self._result(len(bars))

    def write_funding(self, rates: list[Any]) -> WriteResult:
        self.funding.extend(rates)
        return self._result(len(rates))

    def write_open_interest(self, points: list[Any]) -> WriteResult:
        self.open_interest.extend(points)
        return self._result(len(points))

    def record_progress(self, **kwargs: Any) -> None:
        self.progress.append(kwargs)

    def kline_bounds(self, symbol: str, interval: KlineInterval) -> tuple[int, int] | None:
        rows = [b for b in self.klines if b.symbol == symbol and b.interval is interval]
        if not rows:
            return None
        return min(b.start_ms for b in rows), max(b.start_ms for b in rows)

    def count_klines(self, symbol: str, interval: KlineInterval) -> int:
        return len([b for b in self.klines if b.symbol == symbol and b.interval is interval])


def make(responses: list[dict[str, Any]]) -> tuple[HistoryBackfill, FakeStorage]:
    queue = list(responses)

    def transport(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
        return queue.pop(0) if queue else ok([])

    client = BybitHistoryClient(
        "https://api.test", http_get=transport,
        min_request_interval_seconds=0.0, sleep=lambda _: None,
    )
    storage = FakeStorage()
    ticks = iter(range(0, 10_000))
    return HistoryBackfill(client, storage, monotonic=lambda: float(next(ticks))), storage


# ------------------------------------------------------------------ backfill


def test_klines_are_written_and_counted() -> None:
    backfill, storage = make([ok([bar(0), bar(MINUTE_MS)])])
    report = backfill.klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + 2 * MINUTE_MS)

    assert report.received == 2
    assert report.inserted == 2
    assert report.pages == 1
    assert report.complete is True
    assert len(storage.klines) == 2


def test_progress_is_recorded_after_each_series() -> None:
    backfill, storage = make([ok([bar(0)])])
    backfill.klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + MINUTE_MS)

    assert storage.progress[0]["series"] == SERIES_KLINE
    assert storage.progress[0]["symbol"] == "BTCUSDT"
    assert storage.progress[0]["covered_from_ms"] == T0
    assert storage.progress[0]["covered_to_ms"] == T0 + MINUTE_MS


def test_duplicate_rows_are_reported_not_hidden() -> None:
    backfill, storage = make([ok([bar(0), bar(MINUTE_MS)])])
    storage.duplicates = 2
    report = backfill.klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + 2 * MINUTE_MS)

    assert report.received == 2
    assert report.inserted == 0
    assert report.duplicates == 2


def test_progress_hook_receives_each_report() -> None:
    seen: list[SeriesReport] = []
    backfill, _ = make([ok([bar(0)])])
    backfill.progress = seen.append
    backfill.klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + MINUTE_MS)

    assert len(seen) == 1
    assert "kline/BTCUSDT/1" in seen[0].summary()


def test_funding_backfill_records_settlement_interval() -> None:
    backfill, storage = make(
        [ok([{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": "1"}])]
    )
    report = backfill.funding("BTCUSDT", 0, 3_600_000)

    assert report.series == SERIES_FUNDING
    assert report.interval == "settlement"
    assert len(storage.funding) == 1


def test_open_interest_backfill_writes_points() -> None:
    backfill, storage = make([ok([{"openInterest": "5", "timestamp": "1"}])])
    report = backfill.open_interest("BTCUSDT", OpenInterestInterval.ONE_HOUR, 0, 3_600_000)

    assert report.received == 1
    assert len(storage.open_interest) == 1


def test_one_connection_is_held_for_the_whole_series() -> None:
    """Many pages must not mean many connections."""
    window = 1000 * MINUTE_MS
    backfill, storage = make(
        [ok([bar(0)]), ok([bar(window)]), ok([bar(2 * window)])]
    )
    report = backfill.klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + 3 * window)

    assert report.pages == 3
    assert storage.sessions_opened == 1


def test_empty_range_still_records_progress() -> None:
    backfill, storage = make([ok([])])
    report = backfill.klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + MINUTE_MS)

    assert report.received == 0
    assert report.complete is True
    assert len(storage.progress) == 1


# ---------------------------------------------------------------- gap report


def test_gap_report_detects_missing_bars() -> None:
    backfill, _ = make([ok([bar(0), bar(4 * MINUTE_MS)])])
    backfill.klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + 5 * MINUTE_MS)
    gap = backfill.gap_report("BTCUSDT", KlineInterval.ONE_MINUTE)

    assert gap.stored_rows == 2
    assert gap.expected_rows == 5
    assert gap.missing_rows == 3
    assert gap.coverage_pct == pytest.approx(40.0)


def test_gap_report_is_complete_for_contiguous_series() -> None:
    backfill, _ = make([ok([bar(i * MINUTE_MS) for i in range(5)])])
    backfill.klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + 5 * MINUTE_MS)
    gap = backfill.gap_report("BTCUSDT", KlineInterval.ONE_MINUTE)

    assert gap.missing_rows == 0
    assert gap.coverage_pct == pytest.approx(100.0)


def test_gap_report_handles_empty_store() -> None:
    backfill, _ = make([])
    gap = backfill.gap_report("BTCUSDT", KlineInterval.ONE_MINUTE)

    assert gap.stored_rows == 0
    assert gap.expected_rows == 0
    assert gap.first_ms is None


# ------------------------------------------------------------------- dsn/cli


@pytest.mark.parametrize(
    "given, expected",
    [
        ("postgresql+psycopg://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql://u:p@h:5432/db"),
    ],
)
def test_psycopg_dsn_normalization(given: str, expected: str) -> None:
    assert psycopg_dsn(given) == expected


def test_psycopg_dsn_rejects_psycopg2() -> None:
    with pytest.raises(ValueError, match="psycopg2"):
        psycopg_dsn("postgresql+psycopg2://u:p@h/db")


def test_cli_refuses_to_write_into_production_database() -> None:
    with pytest.raises(SystemExit, match="refusing to write history into production"):
        cli.guard_research_database("postgresql+psycopg://u:p@localhost:5432/bybot")


def test_cli_allows_research_database() -> None:
    dsn = cli.guard_research_database("postgresql+psycopg://u:p@localhost:5432/bybot_claude")
    assert dsn.endswith("/bybot_claude")


def test_cli_guard_ignores_query_suffix() -> None:
    with pytest.raises(SystemExit):
        cli.guard_research_database("postgresql://u:p@h/bybot?sslmode=require")


def test_cli_parses_utc_day() -> None:
    assert cli.parse_day("1970-01-02") == 86_400_000


def test_cli_rejects_inverted_range() -> None:
    with pytest.raises(SystemExit, match="must be earlier"):
        cli.main(
            ["--from", "2026-01-02", "--to", "2026-01-01",
             "--database-url", "postgresql://u:p@h/bybot_claude"]
        )


def test_cli_requires_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit, match="required"):
        cli.main(["--from", "2026-01-01", "--to", "2026-01-02", "--database-url", ""])


def test_default_symbols_match_the_researched_universe() -> None:
    assert len(cli.DEFAULT_SYMBOLS) == 12
    assert "BTCUSDT" in cli.DEFAULT_SYMBOLS
    assert "WIFUSDT" in cli.DEFAULT_SYMBOLS
