from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations

from app.backtest.data import Dataset
from app.backtest.portfolio import PortfolioContext

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class PairsParameters:
    """Selected on training folds only; never on the holdout."""

    lookback_hours: int = 24 * 21
    entry_z: float = 2.0
    exit_z: float = 0.5
    max_pairs: int = 3
    notional_per_leg: float = 1_000.0
    rebalance_hours: int = 24
    min_observations: int = 200
    max_holding_hours: int = 24 * 30


@dataclass
class _PairSeries:
    """Log-spread of one pair with prefix sums for O(1) windows."""

    left: str
    right: str
    spread: list[float] = field(default_factory=list)
    prefix: list[float] = field(default_factory=list)
    prefix_sq: list[float] = field(default_factory=list)
    valid_from: int = 0

    def window(self, index: int, length: int) -> tuple[float, float] | None:
        """Mean and standard deviation over the trailing window."""
        high = index + 1
        low = max(self.valid_from, high - length)
        count = high - low
        if count < 2:
            return None
        total = self.prefix[high] - self.prefix[low]
        total_sq = self.prefix_sq[high] - self.prefix_sq[low]
        mean = total / count
        variance = total_sq / count - mean * mean
        if variance <= 0:
            return None
        return mean, math.sqrt(variance)


class PairsStrategy:
    """Mean reversion of the log spread between two correlated perpetuals.

    Economic claim: assets driven by common factors drift apart on flow and
    converge afterwards. Unlike the hypotheses already rejected, this trades a
    spread rather than a direction, so it needs no speed advantage and holds
    for days, which is the only way a 13 bps round trip amortises.

    The honest weakness is stated up front: crypto cointegration is unstable
    and breaks hardest exactly when a position is largest. That is what the
    frozen holdout and the drawdown gate are for.
    """

    name = "pairs"

    def __init__(self, parameters: PairsParameters) -> None:
        self.parameters = parameters
        self._pairs: list[_PairSeries] = []
        self._index: dict[int, int] = {}
        self._anchor_ms: int | None = None
        # symbol -> signed notional, and pair -> direction, opened_ms
        self._open: dict[tuple[str, str], tuple[int, int]] = {}

    # ------------------------------------------------------------- preparation

    def prepare(self, dataset: Dataset) -> None:
        timeline = dataset.timeline
        self._anchor_ms = timeline[0] if timeline else None
        self._index = {stamp: position for position, stamp in enumerate(timeline)}
        self._open = {}

        logs: dict[str, list[float | None]] = {}
        for symbol, history in dataset.symbols.items():
            series: list[float | None] = []
            last: float | None = None
            for stamp in timeline:
                bar = history.perp_at(stamp)
                if bar is not None and bar.close > 0:
                    last = math.log(bar.close)
                # Carry the last observed price across a missing bar. This uses
                # only past data; it never reaches forward to fill a hole.
                series.append(last)
            logs[symbol] = series

        self._pairs = []
        for left, right in combinations(sorted(dataset.symbols), 2):
            left_series, right_series = logs[left], logs[right]
            spread: list[float] = []
            valid_from = len(timeline)
            for position in range(len(timeline)):
                a, b = left_series[position], right_series[position]
                if a is None or b is None:
                    spread.append(0.0)
                    continue
                if position < valid_from:
                    valid_from = position
                spread.append(a - b)

            prefix, prefix_sq = [0.0], [0.0]
            for value in spread:
                prefix.append(prefix[-1] + value)
                prefix_sq.append(prefix_sq[-1] + value * value)

            self._pairs.append(
                _PairSeries(
                    left=left, right=right, spread=spread,
                    prefix=prefix, prefix_sq=prefix_sq, valid_from=valid_from,
                )
            )

    # ---------------------------------------------------------------- decision

    def _is_rebalance_bar(self, timestamp_ms: int) -> bool:
        if self._anchor_ms is None:
            return False
        return (timestamp_ms - self._anchor_ms) % (
            self.parameters.rebalance_hours * HOUR_MS
        ) == 0

    def zscore(self, pair: _PairSeries, position: int) -> float | None:
        parameters = self.parameters
        length = parameters.lookback_hours
        if position - pair.valid_from < parameters.min_observations:
            return None
        stats = pair.window(position, length)
        if stats is None:
            return None
        mean, deviation = stats
        return (pair.spread[position] - mean) / deviation

    def decide(self, context: PortfolioContext) -> dict[str, float]:
        if not self._is_rebalance_bar(context.timestamp_ms):
            return dict(context.positions)

        position = self._index.get(context.timestamp_ms)
        if position is None:
            return dict(context.positions)

        parameters = self.parameters
        candidates: list[tuple[float, _PairSeries, int]] = []

        for pair in self._pairs:
            score = self.zscore(pair, position)
            if score is None:
                continue
            key = (pair.left, pair.right)
            held = self._open.get(key)

            if held is not None:
                direction, opened_ms = held
                aged_out = (
                    context.timestamp_ms - opened_ms
                    > parameters.max_holding_hours * HOUR_MS
                )
                # Close on convergence, on a stop-out, or when the spread has
                # simply refused to revert for long enough.
                if abs(score) < parameters.exit_z or aged_out or abs(score) > 4.0:
                    self._open.pop(key, None)
                    continue
                candidates.append((abs(score), pair, direction))
                continue

            if abs(score) >= parameters.entry_z:
                # A positive z means the left leg is rich relative to the right.
                direction = -1 if score > 0 else 1
                candidates.append((abs(score), pair, direction))

        candidates.sort(key=lambda item: -item[0])
        chosen = candidates[: parameters.max_pairs]

        targets: dict[str, float] = {}
        active: dict[tuple[str, str], tuple[int, int]] = {}
        for _, pair, direction in chosen:
            key = (pair.left, pair.right)
            opened = self._open.get(key, (direction, context.timestamp_ms))[1]
            active[key] = (direction, opened)
            size = parameters.notional_per_leg
            targets[pair.left] = targets.get(pair.left, 0.0) + direction * size
            targets[pair.right] = targets.get(pair.right, 0.0) - direction * size

        self._open = active
        return {symbol: value for symbol, value in targets.items() if abs(value) > 1e-9}
