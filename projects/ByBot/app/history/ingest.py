from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.history.client import BybitHistoryClient
from app.history.models import KlineInterval, OpenInterestInterval
from app.history.storage import HistoryStorage, WriteResult

ProgressHook = Callable[["SeriesReport"], None]

SERIES_KLINE = "kline"
SERIES_FUNDING = "funding_rate"
SERIES_OPEN_INTEREST = "open_interest"


@dataclass
class SeriesReport:
    series: str
    symbol: str
    interval: str
    requested_from_ms: int
    requested_to_ms: int
    pages: int = 0
    received: int = 0
    inserted: int = 0
    duplicates: int = 0
    elapsed_seconds: float = 0.0
    complete: bool = False

    def summary(self) -> str:
        rate = self.received / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0
        return (
            f"{self.series}/{self.symbol}/{self.interval}: "
            f"pages={self.pages} received={self.received} inserted={self.inserted} "
            f"dup={self.duplicates} in {self.elapsed_seconds:.1f}s ({rate:.0f} rows/s)"
        )


@dataclass
class GapReport:
    symbol: str
    interval: str
    stored_rows: int
    expected_rows: int
    first_ms: int | None
    last_ms: int | None

    @property
    def missing_rows(self) -> int:
        return max(self.expected_rows - self.stored_rows, 0)

    @property
    def coverage_pct(self) -> float:
        if self.expected_rows <= 0:
            return 0.0
        return self.stored_rows / self.expected_rows * 100.0


@dataclass
class BackfillSummary:
    reports: list[SeriesReport] = field(default_factory=list)

    @property
    def total_inserted(self) -> int:
        return sum(report.inserted for report in self.reports)

    @property
    def total_received(self) -> int:
        return sum(report.received for report in self.reports)

    @property
    def elapsed_seconds(self) -> float:
        return sum(report.elapsed_seconds for report in self.reports)


class HistoryBackfill:
    """Streams Bybit history into research storage, page by page.

    Every page is committed before the next request, so an interrupted run
    resumes from durable state instead of restarting the symbol.
    """

    def __init__(
        self,
        client: BybitHistoryClient,
        storage: HistoryStorage,
        *,
        progress: ProgressHook | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.storage = storage
        self.progress = progress
        self._monotonic = monotonic

    @staticmethod
    def _accumulate(report: SeriesReport, written: "WriteResult") -> None:
        report.pages += 1
        report.received += written.received
        report.inserted += written.inserted
        report.duplicates += written.duplicates

    def _finish(self, report: SeriesReport, started: float) -> SeriesReport:
        report.elapsed_seconds = self._monotonic() - started
        report.complete = True
        self.storage.record_progress(
            series=report.series,
            symbol=report.symbol,
            interval=report.interval,
            covered_from_ms=report.requested_from_ms,
            covered_to_ms=report.requested_to_ms,
            row_count=report.inserted,
        )
        if self.progress is not None:
            self.progress(report)
        return report

    def klines(
        self, symbol: str, interval: KlineInterval, start_ms: int, end_ms: int
    ) -> SeriesReport:
        report = SeriesReport(
            series=SERIES_KLINE,
            symbol=symbol,
            interval=interval.value,
            requested_from_ms=start_ms,
            requested_to_ms=end_ms,
        )
        started = self._monotonic()
        with self.storage.session():
            for page in self.client.iter_klines(symbol, interval, start_ms, end_ms):
                self._accumulate(report, self.storage.write_klines(page))
        return self._finish(report, started)

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> SeriesReport:
        report = SeriesReport(
            series=SERIES_FUNDING,
            symbol=symbol,
            interval="settlement",
            requested_from_ms=start_ms,
            requested_to_ms=end_ms,
        )
        started = self._monotonic()
        with self.storage.session():
            for page in self.client.iter_funding(symbol, start_ms, end_ms):
                self._accumulate(report, self.storage.write_funding(page))
        return self._finish(report, started)

    def open_interest(
        self, symbol: str, interval: OpenInterestInterval, start_ms: int, end_ms: int
    ) -> SeriesReport:
        report = SeriesReport(
            series=SERIES_OPEN_INTEREST,
            symbol=symbol,
            interval=interval.value,
            requested_from_ms=start_ms,
            requested_to_ms=end_ms,
        )
        started = self._monotonic()
        with self.storage.session():
            for page in self.client.iter_open_interest(symbol, interval, start_ms, end_ms):
                self._accumulate(report, self.storage.write_open_interest(page))
        return self._finish(report, started)

    def gap_report(self, symbol: str, interval: KlineInterval) -> GapReport:
        """Compare stored bars against the bars implied by the stored range.

        A perpetual trades continuously, so a healthy 1m series should have one
        bar per interval. Missing bars are reported, never interpolated.
        """
        bounds = self.storage.kline_bounds(symbol, interval)
        stored = self.storage.count_klines(symbol, interval)
        if bounds is None:
            return GapReport(symbol, interval.value, stored, 0, None, None)
        first_ms, last_ms = bounds
        expected = (last_ms - first_ms) // interval.milliseconds + 1
        return GapReport(symbol, interval.value, stored, expected, first_ms, last_ms)
