from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from app.backtest.costs import CostModel, Liquidity
from app.backtest.data import Dataset

BPS = Decimal("10000")


@dataclass(frozen=True)
class BarContext:
    """Everything a strategy may see at one decision point.

    The engine hands over only data at or before `timestamp_ms`. A strategy
    cannot reach the future because the future is not in this object.
    """

    timestamp_ms: int
    dataset: Dataset
    open_symbols: frozenset[str]
    equity: float


class CarryStrategy(Protocol):
    def decide(self, context: BarContext) -> dict[str, float]:
        """Return the desired per-leg notional for each symbol to hold."""


@dataclass
class CarryPosition:
    symbol: str
    notional: float
    opened_ms: int
    perp_entry: float
    spot_entry: float
    funding_collected: float = 0.0
    entry_cost: float = 0.0

    def unrealized(self, perp_price: float, spot_price: float) -> float:
        # Short the perpetual, long the spot: the pair earns basis convergence.
        perp_leg = -self.notional * (perp_price / self.perp_entry - 1.0)
        spot_leg = self.notional * (spot_price / self.spot_entry - 1.0)
        return perp_leg + spot_leg


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    opened_ms: int
    closed_ms: int
    notional: float
    price_pnl: float
    funding_pnl: float
    costs: float

    @property
    def net_pnl(self) -> float:
        return self.price_pnl + self.funding_pnl - self.costs

    @property
    def net_bps(self) -> float:
        return self.net_pnl / self.notional * 10_000.0 if self.notional else 0.0

    @property
    def holding_hours(self) -> float:
        return (self.closed_ms - self.opened_ms) / 3_600_000.0


@dataclass
class BacktestResult:
    starting_equity: float
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    funding_events: int = 0
    skipped_missing_leg: int = 0

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_equity

    @property
    def net_pnl(self) -> float:
        return self.final_equity - self.starting_equity


class BacktestEngine:
    """Hourly replay with next-bar execution and explicit two-leg costs."""

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
        self._entry_bps = float(
            costs.entry_bps("perp", liquidity) + costs.entry_bps("spot", liquidity)
        )
        self._exit_bps = float(
            costs.exit_bps("perp", liquidity) + costs.exit_bps("spot", liquidity)
        )

    def run(self, strategy: CarryStrategy, timeline: list[int]) -> BacktestResult:
        result = BacktestResult(starting_equity=self.starting_equity)
        positions: dict[str, CarryPosition] = {}
        realized = 0.0
        pending: dict[str, float] = {}

        for timestamp in timeline:
            # 1. Fill what was decided on the previous bar, at this bar's open.
            if pending:
                realized += self._execute(pending, positions, timestamp, result)
                pending = {}

            # 2. Funding that settled during this hour accrues to open positions.
            for symbol, position in positions.items():
                history = self.dataset.symbols[symbol]
                rate = history.funding.get(timestamp)
                if rate is None:
                    continue
                # Short perpetual receives funding when the rate is positive.
                position.funding_collected += position.notional * float(rate)
                result.funding_events += 1

            # 3. Mark to market on this bar's close.
            unrealized = 0.0
            for symbol, position in positions.items():
                history = self.dataset.symbols[symbol]
                perp = history.perp_at(timestamp)
                spot = history.spot_at(timestamp)
                if perp is None or spot is None:
                    continue
                unrealized += position.unrealized(perp.close, spot.close)
                unrealized += position.funding_collected
                # Entry cost is already sunk; charging it only at close would
                # overstate equity for the whole life of the position.
                unrealized -= position.entry_cost

            equity = self.starting_equity + realized + unrealized
            result.equity_curve.append((timestamp, equity))

            # 4. Decide with data up to and including this bar; fill next bar.
            pending = strategy.decide(
                BarContext(
                    timestamp_ms=timestamp,
                    dataset=self.dataset,
                    open_symbols=frozenset(positions),
                    equity=equity,
                )
            )

        # Close whatever is still open on the final bar, at its close.
        if timeline and positions:
            realized += self._close_all(positions, timeline[-1], result)
            result.equity_curve[-1] = (timeline[-1], self.starting_equity + realized)

        return result

    # ------------------------------------------------------------- execution

    def _execute(
        self,
        targets: dict[str, float],
        positions: dict[str, CarryPosition],
        timestamp: int,
        result: BacktestResult,
    ) -> float:
        realized = 0.0

        for symbol in list(positions):
            if targets.get(symbol, 0.0) <= 0.0:
                realized += self._close(symbol, positions, timestamp, result)

        for symbol, notional in targets.items():
            if notional <= 0.0 or symbol in positions:
                continue
            history = self.dataset.symbols.get(symbol)
            if history is None:
                continue
            perp = history.perp_at(timestamp)
            spot = history.spot_at(timestamp)
            if perp is None or spot is None:
                # No fabricated price: a missing leg means the trade is skipped.
                result.skipped_missing_leg += 1
                continue
            positions[symbol] = CarryPosition(
                symbol=symbol,
                notional=notional,
                opened_ms=timestamp,
                perp_entry=perp.open,
                spot_entry=spot.open,
                entry_cost=notional * self._entry_bps / 10_000.0,
            )
        return realized

    def _close(
        self,
        symbol: str,
        positions: dict[str, CarryPosition],
        timestamp: int,
        result: BacktestResult,
        *,
        at_close: bool = False,
    ) -> float:
        position = positions.pop(symbol)
        history = self.dataset.symbols[symbol]
        perp = history.perp_at(timestamp)
        spot = history.spot_at(timestamp)
        if perp is None or spot is None:
            perp_price, spot_price = position.perp_entry, position.spot_entry
        else:
            perp_price = perp.close if at_close else perp.open
            spot_price = spot.close if at_close else spot.open

        price_pnl = position.unrealized(perp_price, spot_price)
        exit_cost = position.notional * self._exit_bps / 10_000.0
        total_costs = position.entry_cost + exit_cost

        result.trades.append(
            ClosedTrade(
                symbol=symbol,
                opened_ms=position.opened_ms,
                closed_ms=timestamp,
                notional=position.notional,
                price_pnl=price_pnl,
                funding_pnl=position.funding_collected,
                costs=total_costs,
            )
        )
        return price_pnl + position.funding_collected - total_costs

    def _close_all(
        self, positions: dict[str, CarryPosition], timestamp: int, result: BacktestResult
    ) -> float:
        return sum(
            self._close(symbol, positions, timestamp, result, at_close=True)
            for symbol in list(positions)
        )
