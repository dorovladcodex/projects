from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from itertools import product

from app.backtest.costs import CostModel, Liquidity
from app.backtest.data import Dataset
from app.backtest.metrics import Metrics, evaluate_portfolio
from app.backtest.portfolio import PortfolioEngine
from app.backtest.signals import CrossSectionalParameters
from app.backtest.validation import Fold, chronological_folds, split_holdout


@dataclass(frozen=True)
class Grid:
    lookback_hours: tuple[int, ...] = (24 * 3, 24 * 7, 24 * 14)
    rebalance_hours: tuple[int, ...] = (24, 48)
    basket_size: tuple[int, ...] = (2, 3)

    def combinations(self, base: CrossSectionalParameters) -> list[CrossSectionalParameters]:
        return [
            replace(base, lookback_hours=lookback, rebalance_hours=rebalance, basket_size=size)
            for lookback, rebalance, size in product(
                self.lookback_hours, self.rebalance_hours, self.basket_size
            )
        ]


@dataclass
class FoldChoice:
    fold: Fold
    chosen: CrossSectionalParameters
    train: Metrics
    test: Metrics


@dataclass
class SearchReport:
    """Result of a selection *procedure*, not of a hand-picked configuration."""

    choices: list[FoldChoice]
    holdout: Metrics | None
    holdout_parameters: CrossSectionalParameters | None
    grid_size: int

    @property
    def test_metrics(self) -> list[Metrics]:
        return [choice.test for choice in self.choices]

    @property
    def aggregate_test_pnl(self) -> float:
        return sum(choice.test.net_pnl for choice in self.choices)

    @property
    def positive_folds(self) -> int:
        return sum(1 for choice in self.choices if choice.test.expectancy_bps > 0)

    @property
    def selection_stability(self) -> float:
        """Share of folds that picked the most common configuration.

        A procedure that chooses a different configuration every fold has not
        found a parameter, it has fitted noise four times.
        """
        if not self.choices:
            return 0.0
        counts = Counter(choice.chosen for choice in self.choices)
        return counts.most_common(1)[0][1] / len(self.choices)


def _score(metrics: Metrics) -> float:
    """Selection criterion, fixed before the search runs.

    Sharpe rather than raw PnL: a configuration that made its money in one
    violent stretch should not outrank a steadier one on the training window.
    """
    if metrics.trades < 10:
        return float("-inf")
    return metrics.sharpe


def run_search(
    dataset: Dataset,
    strategy_class,
    base_parameters: CrossSectionalParameters,
    costs: CostModel,
    *,
    grid: Grid | None = None,
    fold_count: int = 4,
    holdout_fraction: float = 0.2,
    starting_equity: float = 10_000.0,
    liquidity: Liquidity = Liquidity.TAKER,
) -> SearchReport:
    """Select parameters on each fold's training window only.

    The frozen holdout is evaluated exactly once, at the end, using the
    configuration the procedure chose most often. It never participates in
    selection, which is the only thing that makes its verdict worth anything.
    """
    grid = grid or Grid()
    candidates = grid.combinations(base_parameters)

    development, holdout = split_holdout(dataset.timeline, holdout_fraction)
    folds = chronological_folds(development, fold_count)

    def measure(parameters: CrossSectionalParameters, window: list[int]) -> Metrics:
        strategy = strategy_class(parameters)
        strategy.prepare(dataset)
        engine = PortfolioEngine(
            dataset, costs, starting_equity=starting_equity, liquidity=liquidity
        )
        return evaluate_portfolio(engine.run(strategy, window))

    choices: list[FoldChoice] = []
    for fold in folds:
        train_window = dataset.slice(fold.train_from_ms, fold.train_to_ms + 1)
        test_window = dataset.slice(fold.test_from_ms, fold.test_to_ms + 1)
        if not train_window or not test_window:
            continue

        best_parameters, best_metrics, best_score = None, None, float("-inf")
        for parameters in candidates:
            metrics = measure(parameters, train_window)
            score = _score(metrics)
            if score > best_score:
                best_parameters, best_metrics, best_score = parameters, metrics, score

        if best_parameters is None or best_metrics is None:
            continue
        choices.append(
            FoldChoice(
                fold=fold,
                chosen=best_parameters,
                train=best_metrics,
                test=measure(best_parameters, test_window),
            )
        )

    holdout_metrics: Metrics | None = None
    holdout_parameters: CrossSectionalParameters | None = None
    if holdout and choices:
        holdout_parameters = Counter(choice.chosen for choice in choices).most_common(1)[0][0]
        holdout_metrics = measure(holdout_parameters, holdout)

    return SearchReport(
        choices=choices,
        holdout=holdout_metrics,
        holdout_parameters=holdout_parameters,
        grid_size=len(candidates),
    )
