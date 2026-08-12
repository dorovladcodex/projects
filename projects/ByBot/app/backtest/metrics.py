from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, pstdev

from app.backtest.engine import BacktestResult

HOURS_PER_YEAR = 8_766


@dataclass(frozen=True)
class Metrics:
    trades: int
    wins: int
    net_pnl: float
    gross_profit: float
    gross_loss: float
    funding_pnl: float
    price_pnl: float
    costs: float
    expectancy_bps: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe: float
    median_holding_hours: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else math.inf

    def summary(self) -> str:
        pf = "inf" if math.isinf(self.profit_factor) else f"{self.profit_factor:.3f}"
        return (
            f"trades={self.trades} win={self.win_rate * 100:.1f}% "
            f"net={self.net_pnl:+.2f} PF={pf} exp={self.expectancy_bps:+.2f}bps "
            f"maxDD={self.max_drawdown_pct:.2f}% sharpe={self.sharpe:.2f}"
        )


def _drawdown(curve: list[tuple[int, float]]) -> tuple[float, float]:
    peak, worst, worst_pct = -math.inf, 0.0, 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        drop = peak - equity
        if drop > worst:
            worst = drop
            worst_pct = drop / peak * 100.0 if peak > 0 else 0.0
    return worst, worst_pct


def _sharpe(curve: list[tuple[int, float]]) -> float:
    """Annualised Sharpe from the hourly equity curve, zero risk-free rate."""
    if len(curve) < 3:
        return 0.0
    returns = []
    for (_, previous), (_, current) in zip(curve, curve[1:]):
        if previous <= 0:
            continue
        returns.append(current / previous - 1.0)
    if len(returns) < 3:
        return 0.0
    deviation = pstdev(returns)
    if deviation == 0:
        return 0.0
    return fmean(returns) / deviation * math.sqrt(HOURS_PER_YEAR)


def evaluate(result: BacktestResult) -> Metrics:
    trades = result.trades
    nets = [trade.net_pnl for trade in trades]
    wins = sum(1 for value in nets if value > 0)
    gross_profit = sum(value for value in nets if value > 0)
    gross_loss = -sum(value for value in nets if value < 0)
    holdings = sorted(trade.holding_hours for trade in trades)
    median_holding = holdings[len(holdings) // 2] if holdings else 0.0
    max_dd, max_dd_pct = _drawdown(result.equity_curve)

    return Metrics(
        trades=len(trades),
        wins=wins,
        net_pnl=result.net_pnl,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        funding_pnl=sum(trade.funding_pnl for trade in trades),
        price_pnl=sum(trade.price_pnl for trade in trades),
        costs=sum(trade.costs for trade in trades),
        expectancy_bps=fmean([trade.net_bps for trade in trades]) if trades else 0.0,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct,
        sharpe=_sharpe(result.equity_curve),
        median_holding_hours=median_holding,
    )


@dataclass(frozen=True)
class Gate:
    name: str
    passed: bool
    detail: str


def promotion_gates(folds: list[Metrics], holdout: Metrics | None) -> list[Gate]:
    """Pre-registered pass/fail criteria, checked after the run, never tuned.

    These mirror the gates the V3/V4 labs already apply so a result here is
    comparable with the project's existing evidence standard.
    """
    positive = sum(1 for fold in folds if fold.expectancy_bps > 0)
    total_trades = sum(fold.trades for fold in folds)
    aggregate = sum(fold.net_pnl for fold in folds)
    worst_dd = max((fold.max_drawdown_pct for fold in folds), default=0.0)

    gates = [
        Gate(
            "oos_expectancy_positive",
            all(fold.expectancy_bps > 0 for fold in folds) and bool(folds),
            f"{positive}/{len(folds)} folds positive",
        ),
        Gate(
            "three_of_four_folds_positive",
            positive >= max(3, len(folds) - 1) if folds else False,
            f"{positive}/{len(folds)}",
        ),
        Gate(
            "minimum_30_oos_trades",
            total_trades >= 30,
            f"{total_trades} trades",
        ),
        Gate(
            "aggregate_net_positive",
            aggregate > 0,
            f"net {aggregate:+.2f}",
        ),
        Gate(
            "max_drawdown_under_25pct",
            worst_dd < 25.0,
            f"worst fold DD {worst_dd:.2f}%",
        ),
    ]
    if holdout is not None:
        gates.append(
            Gate(
                "frozen_holdout_positive",
                holdout.expectancy_bps > 0 and holdout.net_pnl > 0,
                f"holdout net {holdout.net_pnl:+.2f}, "
                f"expectancy {holdout.expectancy_bps:+.2f} bps",
            )
        )
    return gates
