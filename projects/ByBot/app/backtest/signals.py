from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from app.backtest.data import Dataset, SymbolHistory
from app.backtest.portfolio import PortfolioContext
from app.backtest.strategies import _FundingIndex

HOUR_MS = 3_600_000
DAY_MS = 86_400_000


@dataclass(frozen=True)
class CrossSectionalParameters:
    """Selected on training folds only; never on the holdout."""

    lookback_hours: int = 24 * 7
    rebalance_hours: int = 24
    basket_size: int = 3
    gross_notional: float = 4_000.0
    min_observations: int = 5
    # Sub-hourly cadence, used when the decision clock is 1m. Overrides
    # rebalance_hours when set.
    rebalance_minutes: int | None = None

    @property
    def rebalance_ms(self) -> int:
        if self.rebalance_minutes is not None:
            return self.rebalance_minutes * 60_000
        return self.rebalance_hours * HOUR_MS

    @property
    def lookback_ms(self) -> int:
        return self.lookback_hours * HOUR_MS


def _price_at_or_before(history: SymbolHistory, timestamp_ms: int) -> float | None:
    """Latest close at or before the timestamp, without crossing a gap blindly."""
    bars = history.perp
    position = bisect_right([bar.start_ms for bar in bars], timestamp_ms) - 1
    if position < 0:
        return None
    bar = bars[position]
    # Refuse a stale reference: more than a day old is not the price we meant.
    if timestamp_ms - bar.start_ms > DAY_MS:
        return None
    return bar.close


class _CrossSectionalBase:
    """Dollar-neutral long/short basket driven by a ranked signal.

    The book is rebalanced on a fixed schedule rather than every bar. Hourly
    rebalancing of a 12-symbol book would pay the 11 bps perpetual round trip
    often enough to bury any signal, which is the same cost trap that sank the
    production strategies.
    """

    name = "cross_sectional"

    def __init__(self, parameters: CrossSectionalParameters) -> None:
        self.parameters = parameters
        self._anchor_ms: int | None = None

    def prepare(self, dataset: Dataset) -> None:
        self._anchor_ms = dataset.timeline[0] if dataset.timeline else None

    def signal(self, dataset: Dataset, symbol: str, timestamp_ms: int) -> float | None:
        raise NotImplementedError

    def _is_rebalance_bar(self, timestamp_ms: int) -> bool:
        if self._anchor_ms is None:
            return False
        elapsed = timestamp_ms - self._anchor_ms
        return elapsed % self.parameters.rebalance_ms == 0

    def decide(self, context: PortfolioContext) -> dict[str, float]:
        if not self._is_rebalance_bar(context.timestamp_ms):
            return dict(context.positions)

        parameters = self.parameters
        ranked: list[tuple[float, str]] = []
        for symbol in context.dataset.symbols:
            if not context.dataset.tradeable(
                symbol, context.timestamp_ms, require_spot=False
            ):
                continue
            value = self.signal(context.dataset, symbol, context.timestamp_ms)
            if value is not None:
                ranked.append((value, symbol))

        size = parameters.basket_size
        if len(ranked) < size * 2:
            # Too few eligible symbols to build both sides; stay flat rather
            # than run a one-sided book that is really a directional bet.
            return {}

        ranked.sort()
        shorts = [symbol for _, symbol in ranked[:size]]
        longs = [symbol for _, symbol in ranked[-size:]]
        per_leg = parameters.gross_notional / (size * 2)

        targets = {symbol: -per_leg for symbol in shorts}
        targets.update({symbol: per_leg for symbol in longs})
        return targets


class FundingTiltStrategy(_CrossSectionalBase):
    """Long the cheapest funding, short the most expensive. No spot leg.

    Economic claim: the same crowding premium the carry trade harvests, but
    captured as a spread between perpetuals so the 20 bps spot round trip is
    never paid. The trade-off is that the book is no longer delta-hedged
    against one asset, only dollar-neutral across several.

    Ranking is ascending, so the base class shorts the low end. Funding is
    negated here so the low end of the ranking is the *high* funding names.
    """

    name = "funding_tilt"

    def prepare(self, dataset: Dataset) -> None:
        super().prepare(dataset)
        self._index = {
            symbol: _FundingIndex.build(history.funding)
            for symbol, history in dataset.symbols.items()
        }

    def signal(self, dataset: Dataset, symbol: str, timestamp_ms: int) -> float | None:
        index = self._index.get(symbol)
        if index is None:
            return None
        lookback_ms = self.parameters.lookback_hours * HOUR_MS
        total, count = index.window(timestamp_ms, lookback_ms)
        if count < self.parameters.min_observations:
            return None
        days = lookback_ms / DAY_MS
        return -(total * 10_000.0 / days)


class MomentumStrategy(_CrossSectionalBase):
    """Long recent winners, short recent losers, across the perpetual universe.

    Economic claim: information diffuses unevenly across crypto assets, so
    relative strength persists for days. Weaker than a funding premium and
    with no structural payer on the other side, which is why it is tested
    second rather than first.
    """

    name = "momentum"

    def signal(self, dataset: Dataset, symbol: str, timestamp_ms: int) -> float | None:
        history = dataset.symbols.get(symbol)
        if history is None:
            return None
        now = _price_at_or_before(history, timestamp_ms)
        then = _price_at_or_before(
            history, timestamp_ms - self.parameters.lookback_hours * HOUR_MS
        )
        if now is None or then is None or then <= 0:
            return None
        return now / then - 1.0
