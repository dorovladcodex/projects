from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import csv
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any, Iterable, TextIO

from app.microstructure.models import (
    CarryCandidate,
    FundingEventRecord,
    HypotheticalQuote,
    HypotheticalTouchOutcome,
    SynchronizedSnapshot,
    TakerCostEstimate,
)


_ARTIFACT_FILE_WRITE_LOCKS_GUARD = RLock()
_ARTIFACT_FILE_WRITE_LOCKS: dict[Path, RLock] = {}


def _artifact_file_write_lock(path: Path) -> RLock:
    """Serialize appends to one artifact file without blocking other datasets."""
    canonical = path.resolve()
    with _ARTIFACT_FILE_WRITE_LOCKS_GUARD:
        lock = _ARTIFACT_FILE_WRITE_LOCKS.get(canonical)
        if lock is None:
            lock = RLock()
            _ARTIFACT_FILE_WRITE_LOCKS[canonical] = lock
        return lock


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class MicrostructureStorage:
    """Artifact-local durable store, isolated from PostgreSQL execution state."""

    def __init__(self, artifact_dir: Path) -> None:
        self.artifact_dir = artifact_dir.resolve()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.database_path = self.artifact_dir / "telemetry.sqlite"
        self.raw_dir = self.artifact_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.columnar_dir = self.artifact_dir / "columnar-csv"
        self.columnar_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._artifact_handles: dict[Path, TextIO] = {}
        self.connection = sqlite3.connect(
            self.database_path, timeout=30, check_same_thread=False
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self._create_schema()

    def close(self) -> None:
        for path, handle in list(self._artifact_handles.items()):
            with _artifact_file_write_lock(path):
                handle.flush()
                handle.close()
        self._artifact_handles.clear()
        with self._lock:
            self.connection.close()

    def _create_schema(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS selected_universe (
            symbol TEXT PRIMARY KEY,
            selected INTEGER NOT NULL,
            score TEXT,
            observed_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS captures (
            capture_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            exchange_timestamp TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            complete INTEGER NOT NULL,
            synchronization_gap_ms TEXT NOT NULL,
            spot_present INTEGER NOT NULL,
            perp_present INTEGER NOT NULL,
            mark_index_present INTEGER NOT NULL,
            funding_present INTEGER NOT NULL,
            predicted_funding_present INTEGER NOT NULL,
            oi_present INTEGER NOT NULL,
            reasons TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_captures_symbol_time
            ON captures(symbol, completed_at);
        CREATE TABLE IF NOT EXISTS carry_opportunities (
            opportunity_id TEXT PRIMARY KEY,
            capture_id TEXT NOT NULL UNIQUE,
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            classification TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_carry_opportunities_time
            ON carry_opportunities(timestamp);
        CREATE TABLE IF NOT EXISTS taker_costs (
            cost_id TEXT PRIMARY KEY,
            capture_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            venue_leg TEXT NOT NULL,
            side TEXT NOT NULL,
            notional_usdt TEXT NOT NULL,
            complete INTEGER NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(capture_id, venue_leg, side, notional_usdt)
        );
        CREATE TABLE IF NOT EXISTS funding_events (
            event_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            funding_timestamp TEXT NOT NULL,
            funding_rate TEXT NOT NULL,
            context_complete INTEGER NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(symbol, funding_timestamp)
        );
        CREATE INDEX IF NOT EXISTS ix_funding_events_symbol_time
            ON funding_events(symbol, funding_timestamp);
        CREATE TABLE IF NOT EXISTS hypothetical_quotes (
            quote_id TEXT PRIMARY KEY,
            capture_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            venue_leg TEXT NOT NULL,
            side TEXT NOT NULL,
            quote_time TEXT NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(capture_id, venue_leg, side)
        );
        CREATE INDEX IF NOT EXISTS ix_hypothetical_quotes_time
            ON hypothetical_quotes(quote_time);
        CREATE TABLE IF NOT EXISTS hypothetical_touch_outcomes (
            quote_id TEXT NOT NULL,
            horizon_seconds INTEGER NOT NULL,
            complete INTEGER NOT NULL,
            would_touch INTEGER NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(quote_id, horizon_seconds)
        );
        CREATE INDEX IF NOT EXISTS ix_touch_outcomes_completion
            ON hypothetical_touch_outcomes(complete, quote_id, horizon_seconds);
        CREATE TABLE IF NOT EXISTS carry_labels (
            label_id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL,
            horizon TEXT NOT NULL,
            notional_usdt TEXT NOT NULL,
            target_at TEXT NOT NULL,
            coverage TEXT NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(opportunity_id, horizon, notional_usdt)
        );
        CREATE TABLE IF NOT EXISTS carry_label_schedule (
            opportunity_id TEXT NOT NULL,
            horizon TEXT NOT NULL,
            next_due_at TEXT NOT NULL,
            PRIMARY KEY(opportunity_id, horizon)
        );
        CREATE INDEX IF NOT EXISTS ix_carry_label_schedule_due
            ON carry_label_schedule(horizon, next_due_at, opportunity_id);
        CREATE TABLE IF NOT EXISTS collection_gaps (
            gap_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            prior_capture_at TEXT NOT NULL,
            current_capture_at TEXT NOT NULL,
            gap_seconds TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS collector_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
        with self._lock, self.connection:
            self.connection.executescript(schema)
        self._backfill_label_schedule()

    def persist_universe(self, decisions: Iterable[dict[str, Any]]) -> None:
        rows = list(decisions)
        with self._lock, self.connection:
            for decision in rows:
                self.connection.execute(
                    """
                    INSERT INTO selected_universe(symbol, selected, score, observed_at, payload)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        selected=excluded.selected,
                        score=excluded.score,
                        observed_at=excluded.observed_at,
                        payload=excluded.payload
                    """,
                    (
                        decision["symbol"], int(bool(decision["selected"])),
                        decision.get("selection_score_min_leg_turnover_usdt"),
                        decision["observed_at"], _json(decision),
                    ),
                )
        self._write_raw("universe", {"decisions": rows, "at": utc_now()})

    def selected_universe(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT payload FROM selected_universe WHERE selected=1 ORDER BY CAST(score AS REAL) DESC, symbol"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_capture(self, row: SynchronizedSnapshot) -> bool:
        availability = row.availability
        values = (
            row.capture_id, row.symbol, row.exchange_timestamp.isoformat(),
            row.snapshot_completed_at.isoformat(), int(row.complete),
            str(row.synchronization_gap_ms), int(row.spot is not None),
            int(row.perpetual is not None),
            int(availability.get("mark_index") == "AVAILABLE"),
            int(availability.get("funding") == "AVAILABLE"),
            int(availability.get("predicted_funding") == "AVAILABLE"),
            int(availability.get("open_interest") == "AVAILABLE"),
            _json(row.quality_reasons), _json(row),
        )
        inserted = self._insert_ignore(
            """
            INSERT OR IGNORE INTO captures(
                capture_id, symbol, exchange_timestamp, completed_at, complete,
                synchronization_gap_ms, spot_present, perp_present,
                mark_index_present, funding_present, predicted_funding_present,
                oi_present, reasons, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        if inserted:
            self._write_raw("captures", row.model_dump(mode="json"), row.snapshot_completed_at)
            self._write_columnar_csv("captures", {
                "capture_id": row.capture_id,
                "symbol": row.symbol,
                "exchange_timestamp": row.exchange_timestamp,
                "local_receive_timestamp": row.local_receive_timestamp,
                "snapshot_completed_at": row.snapshot_completed_at,
                "complete": row.complete,
                "spot_age_ms": row.spot_age_ms,
                "perp_age_ms": row.perp_age_ms,
                "funding_age_ms": row.funding_age_ms,
                "synchronization_gap_ms": row.synchronization_gap_ms,
                "clock_offset_ms": row.clock_offset_ms,
                "perp_mid_vs_spot_mid_bps": row.perp_mid_vs_spot_mid_bps,
                "mark_vs_spot_bps": row.mark_vs_spot_bps,
                "mark_vs_index_bps": row.mark_vs_index_bps,
                "quality_reasons": row.quality_reasons,
                "spot": row.spot,
                "perpetual": row.perpetual,
            }, row.snapshot_completed_at)
        return inserted

    def save_carry_candidate(self, row: CarryCandidate) -> bool:
        inserted = self._insert_ignore(
            """
            INSERT OR IGNORE INTO carry_opportunities(
                opportunity_id, capture_id, symbol, timestamp, classification, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row.opportunity_id, row.capture_id, row.symbol,
                row.timestamp.isoformat(), row.classification, _json(row),
            ),
        )
        if inserted:
            self._persist_label_schedule(row.opportunity_id, row.model_dump(mode="json"))
            self._write_raw("carry-opportunities", row.model_dump(mode="json"), row.timestamp)
            self._write_columnar_csv(
                "carry-opportunities", row.model_dump(mode="json"), row.timestamp
            )
        return inserted

    def save_taker_cost(self, row: TakerCostEstimate, at: datetime) -> bool:
        inserted = self._insert_ignore(
            """
            INSERT OR IGNORE INTO taker_costs(
                cost_id, capture_id, symbol, venue_leg, side, notional_usdt,
                complete, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.cost_id, row.capture_id, row.symbol, row.venue_leg, row.side,
                str(row.notional_usdt), int(not row.blockers), _json(row),
            ),
        )
        if inserted:
            self._write_raw("taker-costs", row.model_dump(mode="json"), at)
            self._write_columnar_csv(
                "taker-costs", row.model_dump(mode="json"), at
            )
        return inserted

    def save_quote(self, row: HypotheticalQuote) -> bool:
        inserted = self._insert_ignore(
            """
            INSERT OR IGNORE INTO hypothetical_quotes(
                quote_id, capture_id, symbol, venue_leg, side, quote_time, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.quote_id, row.capture_id, row.symbol, row.venue_leg,
                row.side, row.quote_time.isoformat(), _json(row),
            ),
        )
        if inserted:
            self._write_raw("hypothetical-quotes", row.model_dump(mode="json"), row.quote_time)
            self._write_columnar_csv(
                "hypothetical-quotes", row.model_dump(mode="json"), row.quote_time
            )
        return inserted

    def save_touch_outcome(self, row: HypotheticalTouchOutcome) -> bool:
        with self._lock, self.connection:
            prior = self.connection.execute(
                "SELECT complete FROM hypothetical_touch_outcomes WHERE quote_id=? AND horizon_seconds=?",
                (row.quote_id, row.horizon_seconds),
            ).fetchone()
            if prior is not None and int(prior["complete"]) == 1:
                return False
            self.connection.execute(
                """
                INSERT INTO hypothetical_touch_outcomes(
                    quote_id, horizon_seconds, complete, would_touch, payload
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(quote_id, horizon_seconds) DO UPDATE SET
                    complete=excluded.complete,
                    would_touch=excluded.would_touch,
                    payload=excluded.payload
                """,
                (
                    row.quote_id, row.horizon_seconds, int(row.complete),
                    int(row.would_touch), _json(row),
                ),
            )
        if row.complete:
            self._write_raw(
                "hypothetical-touch-outcomes",
                row.model_dump(mode="json"), row.evaluated_at,
            )
            self._write_columnar_csv(
                "hypothetical-touch-outcomes",
                row.model_dump(mode="json"), row.evaluated_at,
            )
        return True

    def save_funding_event(self, row: FundingEventRecord) -> bool:
        inserted = self._insert_ignore(
            """
            INSERT OR IGNORE INTO funding_events(
                event_id, symbol, funding_timestamp, funding_rate,
                context_complete, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row.event_id, row.symbol, row.funding_timestamp.isoformat(),
                str(row.funding_rate), int(row.context_coverage == "AVAILABLE"), _json(row),
            ),
        )
        if inserted:
            self._write_raw("funding-events", row.model_dump(mode="json"), row.observed_at)
            self._write_columnar_csv(
                "funding-events", row.model_dump(mode="json"), row.observed_at
            )
        return inserted

    def save_label(
        self,
        *,
        label_id: str,
        opportunity_id: str,
        horizon: str,
        notional_usdt: Decimal,
        target_at: datetime,
        coverage: str,
        payload: dict[str, Any],
    ) -> bool:
        inserted = self._insert_ignore(
            """
            INSERT OR IGNORE INTO carry_labels(
                label_id, opportunity_id, horizon, notional_usdt,
                target_at, coverage, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                label_id, opportunity_id, horizon, str(notional_usdt),
                target_at.isoformat(), coverage, _json(payload),
            ),
        )
        if inserted:
            self._write_raw("carry-labels", payload, utc_now())
            self._write_columnar_csv("carry-labels", payload, utc_now())
        return inserted

    def record_gap(
        self,
        *,
        gap_id: str,
        symbol: str,
        prior_at: datetime,
        current_at: datetime,
        expected_cadence_seconds: int,
    ) -> bool:
        gap = Decimal(str((current_at - prior_at).total_seconds()))
        payload = {
            "gap_id": gap_id,
            "symbol": symbol,
            "prior_capture_at": prior_at.isoformat(),
            "current_capture_at": current_at.isoformat(),
            "gap_seconds": str(gap),
            "expected_cadence_seconds": expected_cadence_seconds,
        }
        inserted = self._insert_ignore(
            """
            INSERT OR IGNORE INTO collection_gaps(
                gap_id, symbol, prior_capture_at, current_capture_at,
                gap_seconds, payload
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                gap_id, symbol, prior_at.isoformat(), current_at.isoformat(),
                str(gap), _json(payload),
            ),
        )
        if inserted:
            self._write_raw("gaps", payload, current_at)
        return inserted

    def last_capture_at(self, symbol: str) -> datetime | None:
        with self._lock:
            value = self.connection.execute(
                "SELECT MAX(completed_at) FROM captures WHERE symbol=?", (symbol,)
            ).fetchone()[0]
        return _parse_time(value) if value else None

    def recent_quotes(self, since: datetime) -> list[HypotheticalQuote]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT payload FROM hypothetical_quotes WHERE quote_time>=? ORDER BY quote_time",
                (since.isoformat(),),
            ).fetchall()
        return [HypotheticalQuote.model_validate(json.loads(row["payload"])) for row in rows]

    def pending_maker_work(
        self,
        *,
        evaluated_at: datetime,
        horizons_seconds: tuple[int, ...],
        recent_window_seconds: int = 130,
        limit: int = 1_000,
    ) -> list[tuple[HypotheticalQuote, int]]:
        """Return only due, unresolved maker horizons in a bounded recent window."""
        if not horizons_seconds or limit <= 0:
            return []
        horizon_values = ",".join("(?)" for _ in horizons_seconds)
        since = evaluated_at - timedelta(seconds=recent_window_seconds)
        statement = f"""
            WITH horizons(horizon_seconds) AS (VALUES {horizon_values})
            SELECT quote.payload AS quote_payload, horizons.horizon_seconds
            FROM hypothetical_quotes quote
            CROSS JOIN horizons
            LEFT JOIN hypothetical_touch_outcomes outcome
              ON outcome.quote_id=quote.quote_id
             AND outcome.horizon_seconds=horizons.horizon_seconds
            WHERE quote.quote_time>=?
              AND julianday(quote.quote_time)
                    + (CAST(horizons.horizon_seconds AS REAL) / 86400.0)
                  <= julianday(?)
              AND (outcome.quote_id IS NULL OR outcome.complete=0)
            ORDER BY quote.quote_time, quote.quote_id, horizons.horizon_seconds
            LIMIT ?
        """
        parameters = (
            *horizons_seconds,
            since.isoformat(),
            evaluated_at.isoformat(),
            limit,
        )
        with self._lock:
            rows = self.connection.execute(statement, parameters).fetchall()
        return [
            (
                HypotheticalQuote.model_validate(json.loads(row["quote_payload"])),
                int(row["horizon_seconds"]),
            )
            for row in rows
        ]

    def pending_maker_count(
        self,
        *,
        evaluated_at: datetime,
        horizons_seconds: tuple[int, ...],
        recent_window_seconds: int = 130,
    ) -> int:
        if not horizons_seconds:
            return 0
        horizon_values = ",".join("(?)" for _ in horizons_seconds)
        since = evaluated_at - timedelta(seconds=recent_window_seconds)
        statement = f"""
            WITH horizons(horizon_seconds) AS (VALUES {horizon_values})
            SELECT COUNT(*)
            FROM hypothetical_quotes quote
            CROSS JOIN horizons
            LEFT JOIN hypothetical_touch_outcomes outcome
              ON outcome.quote_id=quote.quote_id
             AND outcome.horizon_seconds=horizons.horizon_seconds
            WHERE quote.quote_time>=?
              AND julianday(quote.quote_time)
                    + (CAST(horizons.horizon_seconds AS REAL) / 86400.0)
                  <= julianday(?)
              AND (outcome.quote_id IS NULL OR outcome.complete=0)
        """
        with self._lock:
            return int(self.connection.execute(
                statement,
                (*horizons_seconds, since.isoformat(), evaluated_at.isoformat()),
            ).fetchone()[0])

    def capture_at_or_before(
        self, symbol: str, timestamp: datetime,
    ) -> SynchronizedSnapshot | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT payload FROM captures
                WHERE symbol=? AND completed_at<=?
                ORDER BY completed_at DESC LIMIT 1
                """,
                (symbol, timestamp.isoformat()),
            ).fetchone()
        return (
            SynchronizedSnapshot.model_validate(json.loads(row["payload"]))
            if row is not None else None
        )

    def pending_opportunities(
        self, *, mature_before: datetime, limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT payload FROM carry_opportunities
                WHERE timestamp<=? ORDER BY timestamp LIMIT ?
                """,
                (mature_before.isoformat(), limit),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def opportunities_missing_label(
        self,
        *,
        horizon: str,
        notional_usdt: Decimal,
        limit: int = 2_000,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT opportunity.payload
                FROM carry_opportunities opportunity
                WHERE NOT EXISTS (
                    SELECT 1 FROM carry_labels label
                    WHERE label.opportunity_id=opportunity.opportunity_id
                      AND label.horizon=? AND label.notional_usdt=?
                )
                ORDER BY opportunity.timestamp
                LIMIT ?
                """,
                (horizon, str(notional_usdt), limit),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def due_opportunities_missing_label(
        self,
        *,
        horizon: str,
        notional_usdt: Decimal,
        evaluated_at: datetime,
        fixed_delta: timedelta | None = None,
        funding_intervals: int | None = None,
        limit: int = 256,
    ) -> list[dict[str, Any]]:
        """Load only opportunities whose requested unresolved label is mature."""
        if (fixed_delta is None) == (funding_intervals is None):
            raise ValueError("exactly one label maturity specification is required")
        statement = """
            SELECT opportunity.payload
            FROM carry_opportunities opportunity
            JOIN carry_label_schedule schedule
              ON schedule.opportunity_id=opportunity.opportunity_id
             AND schedule.horizon=?
            WHERE schedule.next_due_at<=?
              AND NOT EXISTS (
                  SELECT 1 FROM carry_labels label
                  WHERE label.opportunity_id=opportunity.opportunity_id
                    AND label.horizon=? AND label.notional_usdt=?
              )
            ORDER BY opportunity.timestamp, opportunity.opportunity_id
            LIMIT ?
        """
        with self._lock:
            rows = self.connection.execute(
                statement,
                (
                    horizon,
                    evaluated_at.isoformat(),
                    horizon,
                    str(notional_usdt),
                    limit,
                ),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def due_label_count(
        self,
        *,
        horizon: str,
        notional_usdt: Decimal,
        evaluated_at: datetime,
        fixed_delta: timedelta | None = None,
        funding_intervals: int | None = None,
    ) -> int:
        if (fixed_delta is None) == (funding_intervals is None):
            raise ValueError("exactly one label maturity specification is required")
        statement = """
            SELECT COUNT(*)
            FROM carry_opportunities opportunity
            JOIN carry_label_schedule schedule
              ON schedule.opportunity_id=opportunity.opportunity_id
             AND schedule.horizon=?
            WHERE schedule.next_due_at<=?
              AND NOT EXISTS (
                  SELECT 1 FROM carry_labels label
                  WHERE label.opportunity_id=opportunity.opportunity_id
                    AND label.horizon=? AND label.notional_usdt=?
              )
        """
        with self._lock:
            return int(self.connection.execute(
                statement,
                (
                    horizon,
                    evaluated_at.isoformat(),
                    horizon,
                    str(notional_usdt),
                ),
            ).fetchone()[0])

    def _persist_label_schedule(
        self, opportunity_id: str, payload: dict[str, Any],
    ) -> None:
        rows = _label_schedule_rows(payload)
        with self._lock, self.connection:
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO carry_label_schedule(
                    opportunity_id, horizon, next_due_at
                ) VALUES (?, ?, ?)
                """,
                [(opportunity_id, horizon, due_at.isoformat()) for horizon, due_at in rows],
            )

    def _backfill_label_schedule(self) -> None:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT opportunity.opportunity_id, opportunity.payload
                FROM carry_opportunities opportunity
                WHERE NOT EXISTS (
                    SELECT 1 FROM carry_label_schedule schedule
                    WHERE schedule.opportunity_id=opportunity.opportunity_id
                )
                """
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            self._persist_label_schedule(str(row["opportunity_id"]), payload)

    def has_label(
        self, opportunity_id: str, horizon: str, notional_usdt: Decimal,
    ) -> bool:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT 1 FROM carry_labels
                WHERE opportunity_id=? AND horizon=? AND notional_usdt=?
                """,
                (opportunity_id, horizon, str(notional_usdt)),
            ).fetchone()
        return row is not None

    def captures_between(
        self, symbol: str, start: datetime, end: datetime,
    ) -> list[SynchronizedSnapshot]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT payload FROM captures
                WHERE symbol=? AND completed_at>? AND completed_at<=?
                ORDER BY completed_at
                """,
                (symbol, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [SynchronizedSnapshot.model_validate(json.loads(row["payload"])) for row in rows]

    def funding_between(
        self, symbol: str, start: datetime, end: datetime,
    ) -> list[FundingEventRecord]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT payload FROM funding_events
                WHERE symbol=? AND funding_timestamp>? AND funding_timestamp<=?
                ORDER BY funding_timestamp
                """,
                (symbol, start.isoformat(), end.isoformat()),
            ).fetchall()
        return [FundingEventRecord.model_validate(json.loads(row["payload"])) for row in rows]

    def set_state(self, key: str, value: Any) -> None:
        now = utc_now().isoformat()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO collector_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, _json(value), now),
            )

    def get_state(self, key: str) -> Any | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT value FROM collector_state WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def data_quality(self) -> dict[str, Any]:
        with self._lock:
            captures = self.connection.execute(
                """
                SELECT COUNT(*) total, SUM(complete) complete,
                    SUM(spot_present) spot, SUM(perp_present) perp,
                    SUM(mark_index_present) mark_index, SUM(funding_present) funding,
                    SUM(predicted_funding_present) predicted, SUM(oi_present) oi,
                    MIN(completed_at) first_at, MAX(completed_at) last_at
                FROM captures
                """
            ).fetchone()
            gaps = self.connection.execute(
                "SELECT synchronization_gap_ms FROM captures"
            ).fetchall()
            quote_count = self.connection.execute(
                "SELECT COUNT(*) FROM hypothetical_quotes"
            ).fetchone()[0]
            maker_complete = self.connection.execute(
                "SELECT COUNT(*) FROM hypothetical_touch_outcomes WHERE horizon_seconds=60 AND complete=1"
            ).fetchone()[0]
            label_count = self.connection.execute(
                "SELECT COUNT(*) FROM carry_labels WHERE coverage='AVAILABLE'"
            ).fetchone()[0]
            opportunity_count = self.connection.execute(
                "SELECT COUNT(*) FROM carry_opportunities"
            ).fetchone()[0]
            funding_events = self.connection.execute(
                "SELECT COUNT(*) FROM funding_events"
            ).fetchone()[0]
            first_capture_at = captures["first_at"]
            funding_in_collection = (
                self.connection.execute(
                    "SELECT COUNT(*) FROM funding_events WHERE funding_timestamp>=?",
                    (first_capture_at,),
                ).fetchone()[0]
                if first_capture_at else 0
            )
            funding_context_complete = (
                self.connection.execute(
                    """
                    SELECT COUNT(*) FROM funding_events
                    WHERE funding_timestamp>=? AND context_complete=1
                    """,
                    (first_capture_at,),
                ).fetchone()[0]
                if first_capture_at else 0
            )
            gap_events = self.connection.execute(
                "SELECT COUNT(*) FROM collection_gaps"
            ).fetchone()[0]
        total = int(captures["total"] or 0)

        def coverage(value: Any) -> str:
            return str(Decimal(int(value or 0)) / Decimal(total) * Decimal("100")) if total else "0"

        gap_values = sorted(Decimal(str(row[0])) for row in gaps)
        return {
            "generated_at": utc_now(),
            "captures_total": total,
            "captures_complete": int(captures["complete"] or 0),
            "captures_partial": total - int(captures["complete"] or 0),
            "first_capture_at": captures["first_at"],
            "last_capture_at": captures["last_at"],
            "days_covered": (
                str((_parse_time(captures["last_at"]) - _parse_time(captures["first_at"])).total_seconds() / 86400)
                if captures["first_at"] and captures["last_at"] else "0"
            ),
            "synchronization_gap_ms": {
                key: str(_percentile(gap_values, probability)) if gap_values else None
                for key, probability in (("p50", Decimal("0.50")), ("p90", Decimal("0.90")), ("p95", Decimal("0.95")), ("p99", Decimal("0.99")))
            },
            "spot_book_coverage_pct": coverage(captures["spot"]),
            "perp_book_coverage_pct": coverage(captures["perp"]),
            "mark_index_coverage_pct": coverage(captures["mark_index"]),
            "funding_coverage_pct": coverage(captures["funding"]),
            "predicted_funding_coverage_pct": coverage(captures["predicted"]),
            "open_interest_coverage_pct": coverage(captures["oi"]),
            "maker_60s_telemetry_coverage_pct": (
                str(Decimal(maker_complete) / Decimal(quote_count) * Decimal("100"))
                if quote_count else "0"
            ),
            "future_label_coverage_pct": (
                str(
                    Decimal(label_count)
                    / Decimal(opportunity_count * 32)
                    * Decimal("100")
                )
                if opportunity_count else "0"
            ),
            "funding_events_captured": int(funding_events),
            "funding_events_during_collection": int(funding_in_collection),
            "funding_event_context_coverage_pct": (
                str(
                    Decimal(int(funding_context_complete))
                    / Decimal(int(funding_in_collection)) * Decimal("100")
                )
                if funding_in_collection else "0"
            ),
            "collection_gap_events": int(gap_events),
        }

    def funding_summary(self) -> dict[str, Any]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT symbol, funding_timestamp, funding_rate
                FROM funding_events
                ORDER BY symbol, funding_timestamp
                """
            ).fetchall()
        by_symbol: dict[str, list[tuple[datetime, Decimal]]] = {}
        for row in rows:
            by_symbol.setdefault(str(row["symbol"]), []).append((
                _parse_time(row["funding_timestamp"]),
                Decimal(str(row["funding_rate"])),
            ))
        independent_regimes = 0
        reversals: list[Decimal] = []
        symbol_rows: dict[str, Any] = {}
        for symbol, values in by_symbol.items():
            regimes = 0
            prior_sign: int | None = None
            regime_started: datetime | None = None
            sign_reversals = 0
            for timestamp, rate in values:
                sign = 1 if rate > 0 else -1 if rate < 0 else 0
                if sign == 0:
                    continue
                if prior_sign is None:
                    regimes += 1
                    regime_started = timestamp
                elif sign != prior_sign:
                    regimes += 1
                    sign_reversals += 1
                    if regime_started is not None:
                        reversals.append(Decimal(str(
                            (timestamp - regime_started).total_seconds()
                        )))
                    regime_started = timestamp
                prior_sign = sign
            independent_regimes += regimes
            magnitudes = [abs(rate) for _, rate in values]
            nonzero_signs = [1 if rate > 0 else -1 for _, rate in values if rate != 0]
            same_sign = sum(
                1 for prior, current in zip(nonzero_signs, nonzero_signs[1:])
                if prior == current
            )
            magnitude_retention = sorted(
                abs(current) / abs(prior)
                for (_, prior), (_, current) in zip(values, values[1:])
                if prior != 0
            )
            symbol_rows[symbol] = {
                "funding_events": len(values),
                "independent_sign_regimes": regimes,
                "sign_reversals": sign_reversals,
                "mean_absolute_rate": (
                    str(sum(magnitudes, ZERO) / Decimal(len(magnitudes)))
                    if magnitudes else None
                ),
                "same_sign_transition_pct": (
                    str(
                        Decimal(same_sign) / Decimal(len(nonzero_signs) - 1)
                        * Decimal("100")
                    )
                    if len(nonzero_signs) > 1 else None
                ),
                "magnitude_retention_ratio_p50": (
                    str(_percentile(magnitude_retention, Decimal("0.50")))
                    if magnitude_retention else None
                ),
                "latest_rate": str(values[-1][1]) if values else None,
            }
        reversals.sort()
        return {
            "funding_events": len(rows),
            "independent_funding_regimes": independent_regimes,
            "sign_reversal_count": len(reversals),
            "time_to_sign_reversal_seconds_p50": (
                str(_percentile(reversals, Decimal("0.50"))) if reversals else None
            ),
            "by_symbol": symbol_rows,
        }

    def readiness(self, *, symbol_count: int) -> dict[str, Any]:
        quality = self.data_quality()
        first = (
            _parse_time(quality["first_capture_at"])
            if quality.get("first_capture_at") else utc_now()
        )
        preliminary_at = first + timedelta(days=14)
        preferred_at = first + timedelta(days=30)
        strong_at = first + timedelta(days=60)
        complete_pct = (
            Decimal(quality["captures_complete"]) / Decimal(quality["captures_total"]) * Decimal("100")
            if quality["captures_total"] else ZERO
        )
        sync_p95 = Decimal(str(quality["synchronization_gap_ms"]["p95"] or "Infinity"))
        gates = {
            "at_least_14_calendar_days": Decimal(str(quality["days_covered"])) >= 14,
            "at_least_5_symbols": symbol_count >= 5,
            "at_least_10000_complete_captures": quality["captures_complete"] >= 10_000,
            "complete_capture_coverage_at_least_95pct": complete_pct >= 95,
            "synchronization_p95_at_most_2000ms": sync_p95 <= 2000,
            "spot_book_coverage_at_least_95pct": Decimal(quality["spot_book_coverage_pct"]) >= 95,
            "perp_book_coverage_at_least_95pct": Decimal(quality["perp_book_coverage_pct"]) >= 95,
            "mark_index_coverage_at_least_95pct": Decimal(quality["mark_index_coverage_pct"]) >= 95,
            "funding_coverage_at_least_95pct": Decimal(quality["funding_coverage_pct"]) >= 95,
            "oi_coverage_at_least_95pct": Decimal(quality["open_interest_coverage_pct"]) >= 95,
            "at_least_30_authoritative_in_collection_funding_events": (
                quality["funding_events_during_collection"] >= 30
            ),
            "funding_event_context_coverage_at_least_90pct": Decimal(
                quality["funding_event_context_coverage_pct"]
            ) >= 90,
            "maker_60s_outcome_coverage_at_least_90pct": Decimal(quality["maker_60s_telemetry_coverage_pct"]) >= 90,
            "future_label_coverage_at_least_90pct": Decimal(
                quality["future_label_coverage_pct"]
            ) >= 90,
        }
        return {
            "generated_at": utc_now(),
            "ready_for_frozen_v5_carry_analysis": all(gates.values()),
            "gates": gates,
            "current": quality,
            "minimum_preliminary_target_days": 14,
            "preferred_target_days": 30,
            "stronger_research_target_days": 60,
            "estimated_14_day_ready_at": preliminary_at,
            "estimated_30_day_ready_at": preferred_at,
            "estimated_60_day_ready_at": strong_at,
            "profitability_claim": False,
        }

    def _insert_ignore(self, statement: str, values: tuple[Any, ...]) -> bool:
        with self._lock, self.connection:
            cursor = self.connection.execute(statement, values)
            return cursor.rowcount == 1

    def _write_raw(
        self, record_type: str, payload: Any, at: datetime | None = None,
    ) -> None:
        timestamp = at or utc_now()
        directory = self.raw_dir / record_type
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{timestamp.astimezone(timezone.utc).date().isoformat()}.jsonl"
        with _artifact_file_write_lock(path):
            handle = self._append_handle(path)
            handle.write(_json(payload) + "\n")
            handle.flush()

    def _write_columnar_csv(
        self,
        record_type: str,
        payload: dict[str, Any],
        at: datetime,
    ) -> None:
        directory = self.columnar_dir / record_type
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{at.astimezone(timezone.utc).date().isoformat()}.csv"
        flattened = {
            key: (
                _json(value)
                if isinstance(value, (dict, list, tuple))
                or hasattr(value, "model_dump")
                else str(value) if value is not None else ""
            )
            for key, value in payload.items()
        }
        with _artifact_file_write_lock(path):
            exists = path.exists() and path.stat().st_size > 0
            handle = self._append_handle(path, newline="")
            writer = csv.DictWriter(handle, fieldnames=list(flattened))
            if not exists:
                writer.writeheader()
            writer.writerow(flattened)
            handle.flush()

    def _append_handle(self, path: Path, *, newline: str | None = None) -> TextIO:
        handle = self._artifact_handles.get(path)
        if handle is None or handle.closed:
            handle = path.open(
                "a", encoding="utf-8", newline=newline, buffering=1
            )
            self._artifact_handles[path] = handle
        return handle


def _label_schedule_rows(payload: dict[str, Any]) -> list[tuple[str, datetime]]:
    canonical = payload.get("canonical_opportunity") or {}
    timestamp_value = canonical.get("timestamp") or payload.get("timestamp")
    if not timestamp_value:
        return []
    try:
        timestamp = _parse_time(str(timestamp_value))
    except (TypeError, ValueError):
        return []
    rows = [
        ("12h", timestamp + timedelta(hours=12)),
        ("24h", timestamp + timedelta(hours=24)),
        ("48h", timestamp + timedelta(hours=48)),
        ("72h", timestamp + timedelta(hours=72)),
    ]
    interval_value = canonical.get("funding_interval_hours")
    if interval_value not in (None, ""):
        try:
            interval_hours = Decimal(str(interval_value))
        except (ArithmeticError, ValueError):
            interval_hours = Decimal("0")
        if interval_hours > 0:
            for count in (1, 2, 3, 6):
                rows.append((
                    f"{count}_funding_interval" + ("s" if count != 1 else ""),
                    timestamp + timedelta(
                        seconds=float(interval_hours * Decimal("3600") * count)
                    ),
                ))
    return rows


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _percentile(values: list[Decimal], probability: Decimal) -> Decimal:
    if not values:
        raise ValueError("percentile requires values")
    if len(values) == 1:
        return values[0]
    position = probability * Decimal(len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - Decimal(lower)
    return values[lower] + (values[upper] - values[lower]) * fraction


ZERO = Decimal("0")
