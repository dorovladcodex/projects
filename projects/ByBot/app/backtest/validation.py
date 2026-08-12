from __future__ import annotations

from dataclasses import dataclass

from app.backtest.costs import CostModel, Liquidity
from app.backtest.data import Dataset
from app.backtest.engine import BacktestEngine
from app.backtest.metrics import Metrics, evaluate


@dataclass(frozen=True)
class Fold:
    index: int
    train_from_ms: int
    train_to_ms: int
    test_from_ms: int
    test_to_ms: int


def chronological_folds(timeline: list[int], count: int, *, train_ratio: float = 0.6) -> list[Fold]:
    """Expanding-window folds in calendar order.

    Randomised splits leak across time in a market series, so every fold trains
    strictly before the period it is tested on.
    """
    if count < 1 or len(timeline) < count * 2:
        return []

    folds: list[Fold] = []
    block = len(timeline) // (count + 1)
    for index in range(count):
        train_end = block * (index + 1)
        test_end = min(block * (index + 2), len(timeline))
        train_start = int(train_end * (1.0 - train_ratio)) if index else 0
        if test_end - train_end < 2:
            continue
        folds.append(
            Fold(
                index=index,
                train_from_ms=timeline[train_start],
                train_to_ms=timeline[train_end - 1],
                test_from_ms=timeline[train_end],
                test_to_ms=timeline[test_end - 1],
            )
        )
    return folds


def split_holdout(timeline: list[int], holdout_fraction: float = 0.2) -> tuple[list[int], list[int]]:
    """Freeze the final slice before any parameter is chosen."""
    if not timeline:
        return [], []
    cut = int(len(timeline) * (1.0 - holdout_fraction))
    return timeline[:cut], timeline[cut:]


@dataclass
class WalkForwardReport:
    folds: list[Metrics]
    holdout: Metrics | None
    fold_definitions: list[Fold]

    @property
    def positive_folds(self) -> int:
        return sum(1 for fold in self.folds if fold.expectancy_bps > 0)


def run_walk_forward(
    dataset: Dataset,
    strategy_factory,
    costs: CostModel,
    *,
    fold_count: int = 4,
    holdout_fraction: float = 0.2,
    starting_equity: float = 10_000.0,
    liquidity: Liquidity = Liquidity.TAKER,
    engine_factory=None,
    evaluator=None,
) -> WalkForwardReport:
    """Chronological walk-forward with a frozen tail.

    The engine and evaluator are injectable so the carry pair simulator and the
    rebalanced long/short book are held to exactly the same fold construction,
    holdout freeze and gate set.
    """
    build_engine = engine_factory or (
        lambda: BacktestEngine(
            dataset, costs, starting_equity=starting_equity, liquidity=liquidity
        )
    )
    measure = evaluator or evaluate

    development, holdout = split_holdout(dataset.timeline, holdout_fraction)
    folds = chronological_folds(development, fold_count)

    fold_metrics: list[Metrics] = []
    for fold in folds:
        window = dataset.slice(fold.test_from_ms, fold.test_to_ms + 1)
        strategy = strategy_factory()
        strategy.prepare(dataset)
        fold_metrics.append(measure(build_engine().run(strategy, window)))

    holdout_metrics: Metrics | None = None
    if holdout:
        strategy = strategy_factory()
        strategy.prepare(dataset)
        holdout_metrics = measure(build_engine().run(strategy, holdout))

    return WalkForwardReport(
        folds=fold_metrics, holdout=holdout_metrics, fold_definitions=folds
    )
