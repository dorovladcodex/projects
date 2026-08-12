from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from app.backtest.costs import CostModel, Liquidity
from app.backtest.data import Dataset


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

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_equity

    @property
    def net_pnl(self) -> float:
        return self.final_equity - self.starting_equity


class PortfolioEngine:
    """Dollar-neutral long/short perpetual book with explicit rebalance costs.

    PnL accrues per bar on the notional actually held during that bar, so a
    rebalance costs exactly the traded difference rather than a full round
    trip. Funding is charged the way the exchange charges it: the long side
    pays when the rate is positive, the short side receives.
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
        result = PortfolioResult(starting_equity=self.starting_equity)
        positions: dict[str, float] = {}
        open_episodes: dict[str, Episode] = {}
        previous_close: dict[str, float] = {}
        pnl = 0.0
        pending: dict[str, float] | None = None

        for timestamp in timeline:
            # 1. Price move on the notional held coming into this bar.
            for symbol, notional in positions.items():
                bar = self.dataset.symbols[symbol].perp_at(timestamp)
                last = previous_close.get(symbol)
                if bar is None or last is None or last <= 0:
                    continue
                move = notional * (bar.close / last - 1.0)
                pnl += move
                open_episodes[symbol].price_pnl += move

            # 2. Funding settled during this bar.
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

            # 3. Apply the previous bar's decision at this bar's open.
            if pending is not None:
                pnl -= self._rebalance(
                    pending, positions, open_episodes, timestamp, result
                )
                pending = None

            for symbol in positions:
                bar = self.dataset.symbols[symbol].perp_at(timestamp)
                if bar is not None:
                    previous_close[symbol] = bar.close

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
            pnl -= self._rebalance({}, positions, open_episodes, timeline[-1], result)
            result.equity_curve[-1] = (timeline[-1], self.starting_equity + pnl)

        return result

    def _rebalance(
        self,
        targets: dict[str, float],
        positions: dict[str, float],
        open_episodes: dict[str, Episode],
        timestamp: int,
        result: PortfolioResult,
    ) -> float:
        cost = 0.0
        touched = set(positions) | set(targets)
        changed = False

        for symbol in touched:
            current = positions.get(symbol, 0.0)
            target = targets.get(symbol, 0.0)

            history = self.dataset.symbols.get(symbol)
            if history is None or history.perp_at(timestamp) is None:
                # No price means no fill; carry the existing position forward.
                continue

            delta = target - current
            if abs(delta) < 1e-9:
                continue

            changed = True
            traded = abs(delta)
            fee = traded * self._trade_bps / 10_000.0
            cost += fee
            result.total_costs += fee
            result.total_turnover += traded

            if current == 0.0:
                open_episodes[symbol] = Episode(symbol=symbol, opened_ms=timestamp)
            episode = open_episodes.get(symbol)
            if episode is not None:
                episode.costs += fee
                episode.peak_notional = max(episode.peak_notional, abs(target))

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
        return cost
