from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg

from app.history.models import (
    FundingRate,
    Kline,
    KlineInterval,
    OpenInterest,
    OpenInterestInterval,
)

SCHEMA = "history"

# Research tables live in their own schema and are deliberately absent from
# alembic/versions/: the production chain must keep exactly one head.
DDL_STATEMENTS: tuple[str, ...] = (
    f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.kline (
        symbol      TEXT     NOT NULL,
        interval    TEXT     NOT NULL,
        start_ms    BIGINT   NOT NULL,
        open        NUMERIC  NOT NULL,
        high        NUMERIC  NOT NULL,
        low         NUMERIC  NOT NULL,
        close       NUMERIC  NOT NULL,
        volume      NUMERIC  NOT NULL,
        turnover    NUMERIC  NOT NULL,
        PRIMARY KEY (symbol, interval, start_ms)
    )
    """,
    # Spot lives in its own table rather than a category column on kline: the
    # symbol strings collide (spot BTCUSDT vs linear BTCUSDT), and a separate
    # table keeps the two instruments explicit at every join.
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.spot_kline (
        symbol      TEXT     NOT NULL,
        interval    TEXT     NOT NULL,
        start_ms    BIGINT   NOT NULL,
        open        NUMERIC  NOT NULL,
        high        NUMERIC  NOT NULL,
        low         NUMERIC  NOT NULL,
        close       NUMERIC  NOT NULL,
        volume      NUMERIC  NOT NULL,
        turnover    NUMERIC  NOT NULL,
        PRIMARY KEY (symbol, interval, start_ms)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.funding_rate (
        symbol          TEXT     NOT NULL,
        funding_time_ms BIGINT   NOT NULL,
        funding_rate    NUMERIC  NOT NULL,
        PRIMARY KEY (symbol, funding_time_ms)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.open_interest (
        symbol        TEXT     NOT NULL,
        interval      TEXT     NOT NULL,
        timestamp_ms  BIGINT   NOT NULL,
        open_interest NUMERIC  NOT NULL,
        PRIMARY KEY (symbol, interval, timestamp_ms)
    )
    """,
    # Exchange history contains genuinely malformed bars: a zero-volume bar can
    # carry a price forward as the next bar's open while that bar's low is
    # computed only over traded prices, leaving open outside [low, high]. Those
    # rows are retained here rather than dropped, mirroring the production
    # persistence_quarantine rule that an audit trail is never bypassed.
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.kline_quarantine (
        symbol     TEXT        NOT NULL,
        interval   TEXT        NOT NULL,
        start_ms   BIGINT      NOT NULL,
        category   TEXT        NOT NULL,
        reason     TEXT        NOT NULL,
        raw_row    JSONB       NOT NULL,
        recorded_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (symbol, interval, start_ms, category)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {SCHEMA}.backfill_progress (
        series          TEXT        NOT NULL,
        symbol          TEXT        NOT NULL,
        interval        TEXT        NOT NULL,
        covered_from_ms BIGINT      NOT NULL,
        covered_to_ms   BIGINT      NOT NULL,
        row_count       BIGINT      NOT NULL DEFAULT 0,
        updated_at      TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (series, symbol, interval)
    )
    """,
)
# No secondary index on (symbol, start_ms): every read also knows the interval,
# so the primary key already covers it. The duplicate cost 31.6 bytes per row,
# about 1 GB across a full 1m backfill.


def psycopg_dsn(database_url: str) -> str:
    """Strip the SQLAlchemy driver marker so raw psycopg can consume the URL."""
    value = database_url.strip()
    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    if value.startswith("postgresql+psycopg2://"):
        raise ValueError("psycopg2 URLs are not supported; use psycopg v3")
    return value


@dataclass(frozen=True)
class WriteResult:
    received: int
    inserted: int

    @property
    def duplicates(self) -> int:
        return self.received - self.inserted


@dataclass(frozen=True)
class Coverage:
    series: str
    symbol: str
    interval: str
    covered_from_ms: int
    covered_to_ms: int
    row_count: int


class HistoryStorage:
    """PostgreSQL store for historical research series.

    It targets a dedicated research database and never writes to the tables
    owned by the production runtime.
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = psycopg_dsn(dsn)
        self._session: psycopg.Connection | None = None

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        if self._session is not None:
            yield self._session
            return
        with psycopg.connect(self.dsn) as connection:
            yield connection

    @contextmanager
    def session(self) -> Iterator["HistoryStorage"]:
        """Hold one connection open across many writes.

        A multi-year backfill issues tens of thousands of page writes; opening a
        connection per page dominates the cost and exhausts server slots.
        """
        if self._session is not None:
            yield self
            return
        with psycopg.connect(self.dsn) as connection:
            self._session = connection
            try:
                yield self
            finally:
                self._session = None

    def create_schema(self) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                for statement in DDL_STATEMENTS:
                    cursor.execute(statement)
            connection.commit()

    # ---------------------------------------------------------------- writes

    def _bulk_upsert(
        self,
        connection: psycopg.Connection,
        table: str,
        columns: Sequence[str],
        rows: Sequence[tuple[Any, ...]],
    ) -> WriteResult:
        if not rows:
            return WriteResult(received=0, inserted=0)

        column_list = ", ".join(columns)
        staging = f"staging_{table}"
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE TEMP TABLE {staging} "
                f"(LIKE {SCHEMA}.{table} INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            with cursor.copy(f"COPY {staging} ({column_list}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            cursor.execute(
                f"INSERT INTO {SCHEMA}.{table} ({column_list}) "
                f"SELECT {column_list} FROM {staging} ON CONFLICT DO NOTHING"
            )
            inserted = cursor.rowcount
            cursor.execute(f"DROP TABLE {staging}")
        return WriteResult(received=len(rows), inserted=max(inserted, 0))

    def write_klines(self, bars: Sequence[Kline]) -> WriteResult:
        rows = [
            (
                bar.symbol,
                bar.interval.value,
                bar.start_ms,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.turnover,
            )
            for bar in bars
        ]
        with self.connect() as connection:
            result = self._bulk_upsert(
                connection,
                "kline",
                ("symbol", "interval", "start_ms", "open", "high", "low", "close", "volume", "turnover"),
                rows,
            )
            connection.commit()
        return result

    def write_spot_klines(self, bars: Sequence[Kline]) -> WriteResult:
        rows = [
            (
                bar.symbol,
                bar.interval.value,
                bar.start_ms,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.turnover,
            )
            for bar in bars
        ]
        with self.connect() as connection:
            result = self._bulk_upsert(
                connection,
                "spot_kline",
                ("symbol", "interval", "start_ms", "open", "high", "low", "close", "volume", "turnover"),
                rows,
            )
            connection.commit()
        return result

    def quarantine_kline(
        self, *, symbol: str, interval: str, start_ms: int, category: str,
        reason: str, raw_row: Any,
    ) -> None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {SCHEMA}.kline_quarantine "
                    f"(symbol, interval, start_ms, category, reason, raw_row, recorded_at) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (
                        symbol, interval, start_ms, category, reason,
                        json.dumps(raw_row, default=str), datetime.now(timezone.utc),
                    ),
                )
            connection.commit()

    def quarantined_count(self) -> int:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT COUNT(*) FROM {SCHEMA}.kline_quarantine")
                row = cursor.fetchone()
        return int(row[0]) if row else 0

    def write_funding(self, rates: Sequence[FundingRate]) -> WriteResult:
        rows = [(rate.symbol, rate.funding_time_ms, rate.funding_rate) for rate in rates]
        with self.connect() as connection:
            result = self._bulk_upsert(
                connection,
                "funding_rate",
                ("symbol", "funding_time_ms", "funding_rate"),
                rows,
            )
            connection.commit()
        return result

    def write_open_interest(self, points: Sequence[OpenInterest]) -> WriteResult:
        rows = [
            (point.symbol, point.interval.value, point.timestamp_ms, point.open_interest)
            for point in points
        ]
        with self.connect() as connection:
            result = self._bulk_upsert(
                connection,
                "open_interest",
                ("symbol", "interval", "timestamp_ms", "open_interest"),
                rows,
            )
            connection.commit()
        return result

    # -------------------------------------------------------------- progress

    def record_progress(
        self,
        *,
        series: str,
        symbol: str,
        interval: str,
        covered_from_ms: int,
        covered_to_ms: int,
        row_count: int,
    ) -> None:
        """Widen the recorded window; a resumed run must never shrink coverage."""
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    INSERT INTO {SCHEMA}.backfill_progress
                        (series, symbol, interval, covered_from_ms, covered_to_ms,
                         row_count, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (series, symbol, interval) DO UPDATE SET
                        covered_from_ms = LEAST(
                            {SCHEMA}.backfill_progress.covered_from_ms, EXCLUDED.covered_from_ms),
                        covered_to_ms = GREATEST(
                            {SCHEMA}.backfill_progress.covered_to_ms, EXCLUDED.covered_to_ms),
                        row_count = {SCHEMA}.backfill_progress.row_count + EXCLUDED.row_count,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        series,
                        symbol,
                        interval,
                        covered_from_ms,
                        covered_to_ms,
                        row_count,
                        datetime.now(timezone.utc),
                    ),
                )
            connection.commit()

    def coverage(self, series: str, symbol: str, interval: str) -> Coverage | None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT series, symbol, interval, covered_from_ms, covered_to_ms, row_count
                    FROM {SCHEMA}.backfill_progress
                    WHERE series = %s AND symbol = %s AND interval = %s
                    """,
                    (series, symbol, interval),
                )
                row = cursor.fetchone()
        return Coverage(*row) if row else None

    def all_coverage(self) -> list[Coverage]:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT series, symbol, interval, covered_from_ms, covered_to_ms, row_count
                    FROM {SCHEMA}.backfill_progress
                    ORDER BY series, symbol, interval
                    """
                )
                rows = cursor.fetchall()
        return [Coverage(*row) for row in rows]

    # ----------------------------------------------------------------- reads

    def kline_bounds(self, symbol: str, interval: KlineInterval) -> tuple[int, int] | None:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT MIN(start_ms), MAX(start_ms) FROM {SCHEMA}.kline "
                    f"WHERE symbol = %s AND interval = %s",
                    (symbol, interval.value),
                )
                row = cursor.fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0]), int(row[1])

    def count_klines(self, symbol: str, interval: KlineInterval) -> int:
        with self.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {SCHEMA}.kline WHERE symbol = %s AND interval = %s",
                    (symbol, interval.value),
                )
                row = cursor.fetchone()
        return int(row[0]) if row else 0
