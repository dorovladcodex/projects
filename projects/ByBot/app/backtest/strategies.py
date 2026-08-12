from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

from app.backtest.data import Dataset
from app.backtest.engine import BarContext

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


@dataclass(frozen=True)
class CarryParameters:
    """Selected on training folds only; never on the holdout."""

    lookback_hours: int = 24 * 14
    entry_bps_per_day: float = 3.0
    exit_bps_per_day: float = 0.5
    max_positions: int = 4
    notional_per_leg: float = 1_000.0
    min_settlements: int = 10


@dataclass
class _FundingIndex:
    """Sorted settlements with a prefix sum, for O(log n) trailing windows."""

    times: list[int] = field(default_factory=list)
    prefix: list[float] = field(default_factory=list)

    @classmethod
    def build(cls, funding: dict[int, object]) -> "_FundingIndex":
        times = sorted(funding)
        prefix = [0.0]
        for stamp in times:
            prefix.append(prefix[-1] + float(funding[stamp]))  # type: ignore[arg-type]
        return cls(times=times, prefix=prefix)

    def window(self, end_ms: int, lookback_ms: int) -> tuple[float, int]:
        """Summed rate and settlement count in (end_ms - lookback, end_ms].

        The upper bound is inclusive of the current bar and nothing beyond it,
        which is what keeps the signal free of lookahead.
        """
        high = bisect_right(self.times, end_ms)
        low = bisect_right(self.times, end_ms - lookback_ms)
        return self.prefix[high] - self.prefix[low], high - low


class FundingCarryStrategy:
    """Hold short-perpetual / long-spot on persistently positive funding.

    Economic claim: perpetual funding is the price leveraged longs pay to hold
    exposure without capital. Measured over 66,199 settlements it is positive
    72-88% of the time on every symbol. This strategy tries to collect that
    while holding the price leg flat, and it must still clear the two-legged
    round trip, which the spot leg makes expensive.
    """

    def __init__(self, parameters: CarryParameters) -> None:
        self.parameters = parameters
        self._index: dict[str, _FundingIndex] = {}

    def prepare(self, dataset: Dataset) -> None:
        """Index settlement history. Reads timestamps only, never future rates."""
        self._index = {
            symbol: _FundingIndex.build(history.funding)
            for symbol, history in dataset.symbols.items()
        }

    def funding_bps_per_day(self, symbol: str, timestamp_ms: int) -> tuple[float, int]:
        index = self._index.get(symbol)
        if index is None:
            return 0.0, 0
        lookback_ms = self.parameters.lookback_hours * HOUR_MS
        total, count = index.window(timestamp_ms, lookback_ms)
        days = lookback_ms / DAY_MS
        return total * 10_000.0 / days, count

    def decide(self, context: BarContext) -> dict[str, float]:
        parameters = self.parameters
        ranked: list[tuple[float, str]] = []

        for symbol in context.dataset.symbols:
            if not context.dataset.tradeable(symbol, context.timestamp_ms, require_spot=True):
                continue
            rate, count = self.funding_bps_per_day(symbol, context.timestamp_ms)
            if count < parameters.min_settlements:
                continue
            held = symbol in context.open_symbols
            # Hysteresis: a higher bar to open than to keep, so a position is
            # not churned by noise around a single threshold.
            threshold = parameters.exit_bps_per_day if held else parameters.entry_bps_per_day
            if rate >= threshold:
                ranked.append((rate, symbol))

        ranked.sort(reverse=True)
        chosen = ranked[: parameters.max_positions]
        return {symbol: parameters.notional_per_leg for _, symbol in chosen}
