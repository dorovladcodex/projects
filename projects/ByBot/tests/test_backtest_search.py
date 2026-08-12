from __future__ import annotations

import math

import pytest

from app.backtest.costs import CostModel
from app.backtest.search import Grid, run_search
from app.backtest.signals import CrossSectionalParameters, MomentumStrategy
from tests.test_backtest_engine import T0, HOUR, dataset_of, free, history


def universe(bars: int = 1_200, period: int = 120):
    """Four symbols whose relative strength rotates.

    Static trends would leave the book untouched forever and produce almost no
    episodes, which is not what a selection procedure is meant to be tested on.
    """
    symbols = ("AUSDT", "BUSDT", "CUSDT", "DUSDT")
    return dataset_of(
        *[
            history(
                symbol,
                [
                    100.0 + 10.0 * math.sin(2 * math.pi * bar / period + index * math.pi / 2)
                    for bar in range(bars)
                ],
            )
            for index, symbol in enumerate(symbols)
        ]
    )


def small_grid() -> Grid:
    return Grid(lookback_hours=(24, 48), rebalance_hours=(24,), basket_size=(1,))


# ------------------------------------------------------------------- grid


def test_grid_expands_to_the_cartesian_product() -> None:
    grid = Grid(lookback_hours=(24, 48), rebalance_hours=(12, 24), basket_size=(2, 3))
    combinations = grid.combinations(CrossSectionalParameters())

    assert len(combinations) == 8
    assert len({(c.lookback_hours, c.rebalance_hours, c.basket_size) for c in combinations}) == 8


def test_grid_preserves_untouched_base_fields() -> None:
    base = CrossSectionalParameters(gross_notional=7_777.0, min_observations=9)
    for candidate in small_grid().combinations(base):
        assert candidate.gross_notional == 7_777.0
        assert candidate.min_observations == 9


# --------------------------------------------------------------- procedure


def test_search_selects_per_fold_and_reports_out_of_sample() -> None:
    data = universe()
    report = run_search(
        data, MomentumStrategy, CrossSectionalParameters(), free(),
        grid=small_grid(), fold_count=3,
    )

    assert report.choices, "the search must evaluate at least one fold"
    assert report.grid_size == 2
    for choice in report.choices:
        # Selection happens strictly before the window it is scored on.
        assert choice.fold.train_to_ms < choice.fold.test_from_ms


def test_holdout_is_scored_once_with_the_most_selected_configuration() -> None:
    data = universe()
    report = run_search(
        data, MomentumStrategy, CrossSectionalParameters(), free(),
        grid=small_grid(), fold_count=3,
    )

    assert report.holdout is not None
    assert report.holdout_parameters is not None
    chosen = [choice.chosen for choice in report.choices]
    assert report.holdout_parameters in chosen


def test_selection_stability_is_one_when_every_fold_agrees() -> None:
    data = universe()
    report = run_search(
        data, MomentumStrategy, CrossSectionalParameters(), free(),
        grid=Grid(lookback_hours=(24,), rebalance_hours=(24,), basket_size=(1,)),
        fold_count=3,
    )

    assert report.selection_stability == pytest.approx(1.0)


def test_a_configuration_too_thin_to_trade_is_never_selected() -> None:
    """The score floor keeps a barely-trading configuration from winning."""
    data = universe()
    report = run_search(
        data, MomentumStrategy, CrossSectionalParameters(), free(),
        grid=Grid(lookback_hours=(24,), rebalance_hours=(24, 24 * 40), basket_size=(1,)),
        fold_count=3,
    )

    assert report.choices
    for choice in report.choices:
        assert choice.chosen.rebalance_hours == 24


def test_search_survives_a_dataset_too_short_for_folds() -> None:
    data = dataset_of(history("AUSDT", [100.0] * 4))
    report = run_search(
        data, MomentumStrategy, CrossSectionalParameters(), free(),
        grid=small_grid(), fold_count=4,
    )

    assert report.choices == []
    assert report.holdout is None
    assert report.selection_stability == 0.0


def test_costs_are_applied_during_selection() -> None:
    data = universe()
    free_run = run_search(
        data, MomentumStrategy, CrossSectionalParameters(), free(),
        grid=small_grid(), fold_count=3,
    )
    costly = run_search(
        data, MomentumStrategy, CrossSectionalParameters(), CostModel(),
        grid=small_grid(), fold_count=3,
    )

    assert costly.aggregate_test_pnl < free_run.aggregate_test_pnl
