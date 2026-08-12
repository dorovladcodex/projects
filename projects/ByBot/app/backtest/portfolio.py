from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.backtest.costs import CostModel, Liquidity
from app.backtest.data import Dataset
from app.backtest.execution import Fill, FillStats, build_fill_model


@dataclass(frozen=True)
class PortfolioContext:
    """What a cross-sectional strategy may see at one decision point."""

    timestamp_ms: int
    dataset: Dataset
    positions: dict[str, float]
    equity: float


class PortfolioStrategy(Protocol):
    def prepare(self, dataset: Dataset) -> None: ...

    def decide(self, context: PortfolioContext) -> dict[str, float]:
        """Signed target notional per symbol: positive long, negative short."""


@dataclass
class Episode:
    """One symbol's position from leaving flat until returning to flat."""

    symbol: str
    opened_ms: int
    closed_ms: int = 0
    price_pnl: float = 0.0
    funding_pnl: float = 0.0
    costs: float = 0.0
    peak_notional: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.price_pnl + self.funding_pnl - self.costs

    @property
    def net_bps(self) -> float:
        return self.net_pnl / self.peak_notional * 10_000.0 if self.peak_notional else 0.0

    @property
    def holding_hours(self) -> float:
        return (self.closed_ms - self.opened_ms) / 3_600_000.0


@dataclass
class PortfolioResult:
    starting_equity: float
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)
    total_costs: float = 0.0
    total_funding: float = 0.0
    total_turnover: float = 0.0
    funding_events: int = 0
    rebalances: int = 0
    fills: FillStats = field(default_factory=FillStats)

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_equity

    @property
    def net_pnl(self) -> float:
        return self.final_equity - self.starting_equity


class PortfolioEngine:
    """Dollar-neutral long/short perpetual book with explicit fills and costs.

    A decision taken on bar t is executed on bar t+1. Taker orders cross at
    that bar's open; maker orders rest at the previous close and fill only if
    the bar trades through them, so an unfilled intent simply does not happen.
    PnL is split at the fill price within the execution bar rather than
    assumed to start at its close.
    """

    def __init__(
        self,
        dataset: Dataset,
        costs: CostModel,
        *,
        starting_equity: float = 10_000.0,
        liquidity: Liquidity = Liquidity.TAKER,
    ) -> None:
        self.dataset = dataset
        self.costs = costs
        self.starting_equity = starting_equity
        self.liquidity = liquidity
        self._trade_bps = float(costs.entry_bps("perp", liquidity))

    def run(self, strategy: PortfolioStrategy, timeline: list[int]) -> PortfolioResult:
        fills = build_fill_model(self.liquidity)
        result = PortfolioResult(starting_equity=self.starting_equity)
        positions: dict[str, float] = {}
        open_episodes: dict[str, Episode] = {}
        reference: dict[str, float] = {}
        pnl = 0.0
        pending: dict[str, float] | None = None

        for timestamp in timeline:
            # 1. Execute the previous bar's decision on this bar.
            if pending is not None:
                pnl += self._rebalance(
                    pending, positions, open_episodes, reference, timestamp, result, fills
                )
                pending = None

            # 2. Carry every position to this bar's close from its reference.
            for symbol, notional in positions.items():
                bar = self.dataset.symbols[symbol].perp_at(timestamp)
                base = reference.get(symbol)
                if bar is None or base is None or base <= 0:
                    continue
                move = notional * (bar.close / base - 1.0)
                pnl += move
                open_episodes[symbol].price_pnl += move

            # 3. Funding settled during this bar.
            for symbol, notional in positions.items():
                rate = self.dataset.symbols[symbol].funding.get(timestamp)
                if rate is None:
                    continue
                # Long pays when the rate is positive; short receives.
                amount = -notional * float(rate)
                pnl += amount
                result.total_funding += amount
                open_episodes[symbol].funding_pnl += amount
                result.funding_events += 1

            for symbol in positions:
                bar = self.dataset.symbols[symbol].perp_at(timestamp)
                if bar is not None:
                    reference[symbol] = bar.close

            equity = self.starting_equity + pnl
            result.equity_curve.append((timestamp, equity))

            pending = strategy.decide(
                PortfolioContext(
                    timestamp_ms=timestamp,
                    dataset=self.dataset,
                    positions=dict(positions),
                    equity=equity,
                )
            )

        if timeline and positions:
            pnl += self._rebalance(
                {}, positions, open_episodes, reference, timeline[-1], result, fills,
                force=True,
            )
            result.equity_curve[-1] = (timeline[-1], self.starting_equity + pnl)

        result.fills = fills.stats
        return result

    def _rebalance(
        self,
        targets: dict[str, float],
        positions: dict[str, float],
        open_episodes: dict[str, Episode],
        reference: dict[str, float],
        timestamp: int,
        result: PortfolioResult,
        fills,
        *,
        force: bool = False,
    ) -> float:
        pnl = 0.0
        changed = False

        for symbol in set(positions) | set(targets):
            current = positions.get(symbol, 0.0)
            target = targets.get(symbol, 0.0)
            delta = target - current
            if abs(delta) < 1e-9:
                continue

            history = self.dataset.symbols.get(symbol)
            bar = history.perp_at(timestamp) if history else None
            if bar is None:
                # No price means no fill; the position simply persists.
                continue

            # Where a resting order would sit is the last visible price, which
            # exists even for a symbol the book does not hold yet. That is a
            # different thing from the PnL basis of an existing position.
            resting = history.previous_close(timestamp)
            fill = fills.attempt(bar, resting, buying=delta > 0)
            if fill is None:
                fills.stats.missed_notional += abs(delta)
                if not force:
                    # A missed post-only order is a trade that did not happen.
                    continue
                # The final unwind is not optional: cross the spread to flatten
                # rather than end the backtest holding an unreported position.
                fill = Fill(price=bar.open, liquidity=Liquidity.TAKER)

            base = reference.get(symbol)
            if base is not None and base > 0 and current != 0.0:
                # Split the bar at the fill: the old size earns up to the fill.
                pnl += current * (fill.price / base - 1.0)
                open_episodes[symbol].price_pnl += current * (fill.price / base - 1.0)

            traded = abs(delta)
            fee = traded * self._trade_bps / 10_000.0
            pnl -= fee
            result.total_costs += fee
            result.total_turnover += traded
            changed = True

            if current == 0.0:
                open_episodes[symbol] = Episode(symbol=symbol, opened_ms=timestamp)
            episode = open_episodes.get(symbol)
            if episode is not None:
                episode.costs += fee
                episode.peak_notional = max(episode.peak_notional, abs(target))

            reference[symbol] = fill.price
            if target == 0.0:
                positions.pop(symbol, None)
                if episode is not None:
                    episode.closed_ms = timestamp
                    result.episodes.append(episode)
                    open_episodes.pop(symbol, None)
            else:
                positions[symbol] = target

        if changed:
            result.rebalances += 1
        return pnl
