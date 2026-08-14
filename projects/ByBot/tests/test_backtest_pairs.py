from __future__ import annotations

import math

import pytest

from app.backtest.pairs import PairsParameters, PairsStrategy
from app.backtest.portfolio import PortfolioContext
from tests.test_backtest_engine import T0, HOUR, dataset_of, history


def paired(left: list[float], right: list[float]):
    return dataset_of(history("AUSDT", left), history("BUSDT", right))


def from_spread(values: list[float]):
    """Build a pair whose log spread is exactly `values`.

    Controlling the spread directly keeps the z-score arithmetic legible: the
    baseline alternates by a known amount, so a deviation of a known size lands
    at a known number of standard deviations.
    """
    return paired([100.0 * math.exp(v) for v in values], [100.0] * len(values))


BASELINE = [0.01 if i % 2 else -0.01 for i in range(100)]


def strategy(**overrides) -> PairsStrategy:
    base = dict(
        lookback_hours=48, entry_z=2.0, exit_z=0.5, max_pairs=3,
        notional_per_leg=1_000.0, rebalance_hours=1, min_observations=10,
    )
    base.update(overrides)
    return PairsStrategy(PairsParameters(**base))


def decide(strat: PairsStrategy, data, bar: int):
    return strat.decide(PortfolioContext(T0 + bar * HOUR, data, {}, 10_000.0))


# ------------------------------------------------------------------- spread


def test_stable_ratio_produces_no_signal() -> None:
    """Two assets moving together have nothing to trade."""
    prices = [100.0 + i * 0.1 for i in range(120)]
    data = paired(prices, [p / 2 for p in prices])
    strat = strategy()
    strat.prepare(data)

    assert decide(strat, data, 100) == {}


def test_divergence_opens_the_pair_against_the_rich_leg() -> None:
    # Baseline noise of +/-0.01, then a 0.06 deviation: roughly six sigma.
    data = from_spread(BASELINE + [0.06] * 3 + BASELINE[:20])
    strat = strategy()
    strat.prepare(data)

    targets = decide(strat, data, 101)

    assert targets["AUSDT"] < 0, "the expensive leg is sold"
    assert targets["BUSDT"] > 0, "the cheap leg is bought"


def test_the_book_is_dollar_neutral_per_pair() -> None:
    data = from_spread(BASELINE + [0.06] * 3 + BASELINE[:20])
    strat = strategy()
    strat.prepare(data)

    targets = decide(strat, data, 101)

    assert sum(targets.values()) == pytest.approx(0.0)


def test_convergence_closes_the_pair() -> None:
    data = from_spread(BASELINE + [0.06] * 3 + BASELINE[:40])
    strat = strategy()
    strat.prepare(data)

    assert decide(strat, data, 101) != {}
    # Once the spread is back at the baseline the position is released.
    assert decide(strat, data, 120) == {}


def test_position_is_aged_out_when_the_spread_refuses_to_revert() -> None:
    """A persistent shift is absorbed into the rolling mean, so z stops firing.

    That is exactly when a pairs book quietly holds a broken relationship
    forever, and why the age-out exists rather than relying on the z-score.
    """
    data = from_spread(BASELINE + [0.06] * 200)
    strat = strategy(max_holding_hours=20)
    strat.prepare(data)

    assert decide(strat, data, 101) != {}
    for bar in range(102, 140):
        decide(strat, data, bar)
    assert decide(strat, data, 160) == {}


def test_extreme_divergence_is_stopped_out_rather_than_doubled() -> None:
    """A spread that keeps widening fast is a broken relationship, not an entry."""
    data = from_spread(BASELINE + [0.06] * 2 + [0.30] * 10)
    strat = strategy()
    strat.prepare(data)

    assert decide(strat, data, 100) != {}
    assert decide(strat, data, 103) == {}


# ---------------------------------------------------------------- lookahead


def test_zscore_uses_only_data_up_to_the_decision_bar() -> None:
    data = from_spread(BASELINE + [2.3] * 20)
    strat = strategy()
    strat.prepare(data)

    # At bar 50 the future explosion must be invisible.
    pair = strat._pairs[0]
    early = strat.zscore(pair, 50)

    assert early is None or abs(early) < 1.0


def test_short_history_yields_no_signal() -> None:
    data = paired([100.0] * 20, [100.0] * 20)
    strat = strategy(min_observations=50)
    strat.prepare(data)

    assert decide(strat, data, 15) == {}


def test_missing_bar_carries_the_last_spread_forward_only() -> None:
    left_history = history("AUSDT", [100.0] * 60)
    del left_history.perp[30]
    left_history.build_index()
    data = dataset_of(left_history, history("BUSDT", [100.0] * 60))
    strat = strategy()
    strat.prepare(data)

    pair = strat._pairs[0]
    assert pair.spread[30] == pytest.approx(pair.spread[29])


# ------------------------------------------------------------------ limits


def test_max_pairs_caps_concurrent_positions() -> None:
    data = dataset_of(
        history("AUSDT", [100.0] * 100 + [140.0] * 20),
        history("BUSDT", [100.0] * 100 + [138.0] * 20),
        history("CUSDT", [100.0] * 120),
        history("DUSDT", [100.0] * 120),
    )
    strat = strategy(max_pairs=1)
    strat.prepare(data)

    targets = decide(strat, data, 110)

    assert len(targets) <= 2, "one pair means at most two legs"


def test_rebalance_schedule_is_respected() -> None:
    data = paired([100.0] * 100 + [130.0] * 20, [100.0] * 120)
    strat = strategy(rebalance_hours=24)
    strat.prepare(data)
    held = {"AUSDT": 500.0}

    off = strat.decide(PortfolioContext(T0 + 101 * HOUR, data, held, 10_000.0))
    assert off == held


def test_window_statistics_match_a_direct_calculation() -> None:
    data = paired([100.0 + i for i in range(60)], [100.0] * 60)
    strat = strategy()
    strat.prepare(data)
    pair = strat._pairs[0]

    mean, deviation = pair.window(50, 10)
    manual = pair.spread[41:51]
    expected_mean = sum(manual) / len(manual)

    assert mean == pytest.approx(expected_mean)
    assert deviation == pytest.approx(
        math.sqrt(sum((x - expected_mean) ** 2 for x in manual) / len(manual))
    )
