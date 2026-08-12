from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg

from app.history.storage import SCHEMA, psycopg_dsn

HOUR_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class Bar:
    start_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class SymbolHistory:
    """One symbol's aligned perpetual, spot and funding history."""

    symbol: str
    perp: list[Bar] = field(default_factory=list)
    spot: dict[int, Bar] = field(default_factory=dict)
    funding: dict[int, Decimal] = field(default_factory=dict)
    _index: dict[int, int] = field(default_factory=dict, repr=False)

    def build_index(self) -> None:
        self._index = {bar.start_ms: position for position, bar in enumerate(self.perp)}

    def perp_at(self, start_ms: int) -> Bar | None:
        position = self._index.get(start_ms)
        return self.perp[position] if position is not None else None

    def spot_at(self, start_ms: int) -> Bar | None:
        return self.spot.get(start_ms)

    def previous_close(self, start_ms: int) -> float | None:
        """Close of the bar before this one.

        A resting maker order is placed off the last price the strategy could
        actually see, which exists whether or not the symbol is currently held.
        """
        position = self._index.get(start_ms)
        if position is None or position == 0:
            return None
        return self.perp[position - 1].close

    def has_both_legs(self, start_ms: int) -> bool:
        return start_ms in self._index and start_ms in self.spot

    @property
    def first_ms(self) -> int | None:
        return self.perp[0].start_ms if self.perp else None

    @property
    def last_ms(self) -> int | None:
        return self.perp[-1].start_ms if self.perp else None


@dataclass
class Dataset:
    """All symbols on one shared hourly clock."""

    symbols: dict[str, SymbolHistory] = field(default_factory=dict)
    timeline: list[int] = field(default_factory=list)

    def slice(self, from_ms: int, to_ms: int) -> list[int]:
        """Timestamps in [from_ms, to_ms). Folds are built from this."""
        low = bisect_right(self.timeline, from_ms - 1)
        high = bisect_right(self.timeline, to_ms - 1)
        return self.timeline[low:high]

    def tradeable(self, symbol: str, start_ms: int, *, require_spot: bool) -> bool:
        """A symbol only exists after it lists; never assume otherwise.

        Backtesting the full universe from day one is the most common form of
        lookahead in crypto research: WIF did not exist in 2021.
        """
        history = self.symbols.get(symbol)
        if history is None:
            return False
        if require_spot:
            return history.has_both_legs(start_ms)
        return history.perp_at(start_ms) is not None


def _rows(connection: psycopg.Connection, query: str, params: tuple) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def load_dataset(
    dsn: str,
    symbols: list[str],
    *,
    from_ms: int,
    to_ms: int,
    with_spot: bool = True,
) -> Dataset:
    """Read hourly perpetual, spot and funding history into memory.

    Nothing is interpolated. A missing bar stays missing so the engine can
    decline to trade rather than invent a price.
    """
    dataset = Dataset()
    with psycopg.connect(psycopg_dsn(dsn)) as connection:
        for symbol in symbols:
            history = SymbolHistory(symbol=symbol)

            for start_ms, open_, high, low, close in _rows(
                connection,
                f"SELECT start_ms, open, high, low, close FROM {SCHEMA}.kline "
                f"WHERE symbol=%s AND interval='60' AND start_ms >= %s AND start_ms < %s "
                f"ORDER BY start_ms",
                (symbol, from_ms, to_ms),
            ):
                history.perp.append(
                    Bar(int(start_ms), float(open_), float(high), float(low), float(close))
                )

            if with_spot:
                for start_ms, open_, high, low, close in _rows(
                    connection,
                    f"SELECT start_ms, open, high, low, close FROM {SCHEMA}.spot_kline "
                    f"WHERE symbol=%s AND interval='60' AND start_ms >= %s AND start_ms < %s",
                    (symbol, from_ms, to_ms),
                ):
                    history.spot[int(start_ms)] = Bar(
                        int(start_ms), float(open_), float(high), float(low), float(close)
                    )

            for funding_time_ms, rate in _rows(
                connection,
                f"SELECT funding_time_ms, funding_rate FROM {SCHEMA}.funding_rate "
                f"WHERE symbol=%s AND funding_time_ms >= %s AND funding_time_ms < %s",
                (symbol, from_ms, to_ms),
            ):
                # Attribute each settlement to the hour bucket that contains it.
                bucket = (int(funding_time_ms) // HOUR_MS) * HOUR_MS
                history.funding[bucket] = Decimal(rate)

            history.build_index()
            if history.perp:
                dataset.symbols[symbol] = history

    stamps: set[int] = set()
    for history in dataset.symbols.values():
        stamps.update(bar.start_ms for bar in history.perp)
    dataset.timeline = sorted(stamps)
    return dataset


@dataclass(frozen=True)
class Coverage:
    symbol: str
    perp_bars: int
    spot_bars: int
    funding_events: int
    both_legs: int

    @property
    def spot_coverage_pct(self) -> float:
        if self.perp_bars == 0:
            return 0.0
        return self.both_legs / self.perp_bars * 100.0


def coverage_report(dataset: Dataset) -> list[Coverage]:
    """Expose how much of the perpetual history actually has a spot leg."""
    report: list[Coverage] = []
    for symbol, history in sorted(dataset.symbols.items()):
        both = sum(1 for bar in history.perp if bar.start_ms in history.spot)
        report.append(
            Coverage(
                symbol=symbol,
                perp_bars=len(history.perp),
                spot_bars=len(history.spot),
                funding_events=len(history.funding),
                both_legs=both,
            )
        )
    return report
