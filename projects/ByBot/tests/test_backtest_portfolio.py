from __future__ import annotations

from decimal import Decimal

import pytest

from app.backtest.costs import CostModel, Liquidity
from app.backtest.metrics import evaluate_portfolio
from app.backtest.portfolio import PortfolioContext, PortfolioEngine
from app.backtest.signals import (
    CrossSectionalParameters,
    FundingTiltStrategy,
    MomentumStrategy,
    ReversionStrategy,
)
from tests.test_backtest_engine import T0, HOUR, dataset_of, free, history


class Fixed:
    """Strategy that always requests the same book."""

    def __init__(self, targets: dict[str, float]) -> None:
        self.targets = targets
        self.seen: list[PortfolioContext] = []

    def prepare(self, dataset) -> None:
        return None

    def decide(self, context: PortfolioContext) -> dict[str, float]:
        self.seen.append(context)
        return dict(self.targets)


# ------------------------------------------------------------------ pricing


def test_long_position_gains_when_price_rises() -> None:
    data = dataset_of(history("AUSDT", [100.0, 100.0, 110.0, 110.0]))
    result = PortfolioEngine(data, free()).run(Fixed({"AUSDT": 1_000.0}), data.timeline)

    # Filled at bar 1, +10% into bar 2 on 1000 notional.
    assert result.net_pnl == pytest.approx(100.0)


def test_short_position_gains_when_price_falls() -> None:
    data = dataset_of(history("AUSDT", [100.0, 100.0, 90.0, 90.0]))
    result = PortfolioEngine(data, free()).run(Fixed({"AUSDT": -1_000.0}), data.timeline)

    assert result.net_pnl == pytest.approx(100.0)


def test_dollar_neutral_book_is_flat_when_both_legs_move_together() -> None:
    data = dataset_of(
        history("AUSDT", [100.0, 100.0, 110.0, 110.0]),
        history("BUSDT", [50.0, 50.0, 55.0, 55.0]),
    )
    result = PortfolioEngine(data, free()).run(
        Fixed({"AUSDT": 1_000.0, "BUSDT": -1_000.0}), data.timeline
    )

    assert result.net_pnl == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------------ funding


def test_long_pays_funding_and_short_receives_it() -> None:
    data = dataset_of(history("AUSDT", [100.0] * 4, funding={T0 + 2 * HOUR: "0.001"}))

    long_book = PortfolioEngine(data, free()).run(
        Fixed({"AUSDT": 1_000.0}), data.timeline
    )
    short_book = PortfolioEngine(data, free()).run(
        Fixed({"AUSDT": -1_000.0}), data.timeline
    )

    assert long_book.total_funding == pytest.approx(-1.0)
    assert short_book.total_funding == pytest.approx(1.0)


def test_funding_does_not_accrue_before_the_fill() -> None:
    data = dataset_of(history("AUSDT", [100.0] * 4, funding={T0: "0.01"}))
    result = PortfolioEngine(data, free()).run(Fixed({"AUSDT": 1_000.0}), data.timeline)

    assert result.funding_events == 0


# -------------------------------------------------------------------- costs


def test_rebalance_charges_only_the_traded_difference() -> None:
    """Trimming a position must not cost a full round trip."""
    data = dataset_of(history("AUSDT", [100.0] * 6))
    costs = CostModel(perp_taker_bps=Decimal("5.5"), slippage_bps=Decimal("0"))

    class Ramp:
        def prepare(self, dataset) -> None: ...

        def decide(self, context: PortfolioContext) -> dict[str, float]:
            return {"AUSDT": 1_000.0 if context.timestamp_ms < T0 + 2 * HOUR else 1_200.0}

    result = PortfolioEngine(data, costs).run(Ramp(), data.timeline)

    # 1000 opened, 200 added, 1200 closed at the end = 2400 traded at 5.5 bps.
    assert result.total_turnover == pytest.approx(2_400.0)
    assert result.total_costs == pytest.approx(2_400.0 * 5.5 / 10_000.0)


def test_holding_a_constant_book_incurs_no_rebalance_cost() -> None:
    data = dataset_of(history("AUSDT", [100.0] * 8))
    costs = CostModel(perp_taker_bps=Decimal("5.5"), slippage_bps=Decimal("0"))
    result = PortfolioEngine(data, costs).run(Fixed({"AUSDT": 1_000.0}), data.timeline)

    # Open once, close once. Nothing in between.
    assert result.total_turnover == pytest.approx(2_000.0)


def test_perpetual_only_book_avoids_the_spot_round_trip() -> None:
    """The whole reason these two hypotheses come after carry."""
    costs = CostModel(slippage_bps=Decimal("0"))
    assert costs.round_trip_bps("perp", Liquidity.TAKER) == Decimal("11.0")
    assert costs.carry_round_trip_bps(Liquidity.TAKER) == Decimal("31.0")


# ------------------------------------------------------------- no lookahead


def test_decision_fills_at_the_next_bar() -> None:
    data = dataset_of(history("AUSDT", [100.0, 200.0, 200.0, 200.0]))
    result = PortfolioEngine(data, free()).run(Fixed({"AUSDT": 1_000.0}), data.timeline)

    # Fill happens at bar 1; the 100 -> 200 move must not be captured.
    assert result.net_pnl == pytest.approx(0.0)


def test_positions_passed_to_strategy_exclude_the_pending_decision() -> None:
    data = dataset_of(history("AUSDT", [100.0] * 4))
    strategy = Fixed({"AUSDT": 1_000.0})
    PortfolioEngine(data, free()).run(strategy, data.timeline)

    assert strategy.seen[0].positions == {}
    assert strategy.seen[1].positions == {"AUSDT": 1_000.0}


def test_missing_price_does_not_fill_or_fabricate() -> None:
    item = history("AUSDT", [100.0] * 5)
    del item.perp[1]
    item.build_index()
    data = dataset_of(item)
    result = PortfolioEngine(data, free()).run(Fixed({"AUSDT": 1_000.0}), data.timeline)

    assert result.episodes[0].opened_ms > T0 + HOUR


# --------------------------------------------------------------- episodes


def test_episode_records_costs_and_funding() -> None:
    data = dataset_of(history("AUSDT", [100.0] * 5, funding={T0 + 2 * HOUR: "0.001"}))
    costs = CostModel(perp_taker_bps=Decimal("5.5"), slippage_bps=Decimal("0"))
    result = PortfolioEngine(data, costs).run(Fixed({"AUSDT": -1_000.0}), data.timeline)

    episode = result.episodes[0]
    assert episode.funding_pnl == pytest.approx(1.0)
    assert episode.costs == pytest.approx(2_000.0 * 5.5 / 10_000.0)
    assert episode.holding_hours > 0


def test_portfolio_metrics_use_the_same_summary_shape() -> None:
    data = dataset_of(history("AUSDT", [100.0] * 6, funding={T0 + 2 * HOUR: "0.001"}))
    metrics = evaluate_portfolio(
        PortfolioEngine(data, free()).run(Fixed({"AUSDT": -1_000.0}), data.timeline)
    )

    assert metrics.trades == 1
    assert metrics.funding_pnl == pytest.approx(1.0)
    assert "trades=1" in metrics.summary()


# ------------------------------------------------------------ cross-section


def _universe(returns: dict[str, list[float]], funding: dict[str, str] | None = None):
    items = []
    for symbol, prices in returns.items():
        rate = (funding or {}).get(symbol)
        items.append(
            history(
                symbol, prices,
                funding={T0 + i * HOUR: rate for i in range(0, 48, 8)} if rate else None,
            )
        )
    return dataset_of(*items)


def test_momentum_goes_long_winners_and_short_losers() -> None:
    data = _universe(
        {
            "AUSDT": [100.0] * 40 + [200.0] * 20,   # strongest
            "BUSDT": [100.0] * 40 + [150.0] * 20,
            "CUSDT": [100.0] * 60,
            "DUSDT": [100.0] * 40 + [50.0] * 20,    # weakest
        }
    )
    strategy = MomentumStrategy(
        CrossSectionalParameters(lookback_hours=24, rebalance_hours=24, basket_size=1)
    )
    strategy.prepare(data)
    targets = strategy.decide(PortfolioContext(T0 + 48 * HOUR, data, {}, 10_000.0))

    assert targets["AUSDT"] > 0
    assert targets["DUSDT"] < 0


def test_funding_tilt_shorts_the_most_expensive_funding() -> None:
    data = _universe(
        {name: [100.0] * 60 for name in ("AUSDT", "BUSDT", "CUSDT", "DUSDT")},
        funding={"AUSDT": "0.002", "BUSDT": "0.001", "CUSDT": "0", "DUSDT": "-0.001"},
    )
    strategy = FundingTiltStrategy(
        CrossSectionalParameters(
            lookback_hours=48, rebalance_hours=24, basket_size=1, min_observations=1
        )
    )
    strategy.prepare(data)
    targets = strategy.decide(PortfolioContext(T0 + 48 * HOUR, data, {}, 10_000.0))

    assert targets["AUSDT"] < 0   # highest funding is shorted
    assert targets["DUSDT"] > 0   # cheapest funding is bought


def test_book_is_dollar_neutral() -> None:
    data = _universe(
        {
            "AUSDT": [100.0 + i for i in range(60)],
            "BUSDT": [100.0 + i * 0.5 for i in range(60)],
            "CUSDT": [100.0 - i * 0.2 for i in range(60)],
            "DUSDT": [100.0 - i * 0.5 for i in range(60)],
        }
    )
    strategy = MomentumStrategy(
        CrossSectionalParameters(
            lookback_hours=24, rebalance_hours=24, basket_size=2, gross_notional=4_000.0
        )
    )
    strategy.prepare(data)
    targets = strategy.decide(PortfolioContext(T0 + 48 * HOUR, data, {}, 10_000.0))

    assert sum(targets.values()) == pytest.approx(0.0)
    assert sum(abs(value) for value in targets.values()) == pytest.approx(4_000.0)


def test_strategy_stays_flat_when_the_universe_is_too_small() -> None:
    data = _universe({"AUSDT": [100.0] * 60, "BUSDT": [110.0] * 60})
    strategy = MomentumStrategy(
        CrossSectionalParameters(lookback_hours=24, rebalance_hours=24, basket_size=2)
    )
    strategy.prepare(data)

    assert strategy.decide(PortfolioContext(T0 + 48 * HOUR, data, {}, 10_000.0)) == {}


def test_book_is_only_rebalanced_on_schedule() -> None:
    data = _universe({name: [100.0] * 60 for name in ("AUSDT", "BUSDT", "CUSDT", "DUSDT")})
    strategy = MomentumStrategy(
        CrossSectionalParameters(lookback_hours=24, rebalance_hours=24, basket_size=1)
    )
    strategy.prepare(data)
    held = {"AUSDT": 500.0}

    off_schedule = strategy.decide(PortfolioContext(T0 + 25 * HOUR, data, held, 10_000.0))
    assert off_schedule == held

    on_schedule = strategy.decide(PortfolioContext(T0 + 48 * HOUR, data, held, 10_000.0))
    assert on_schedule != held


def test_reversion_is_exactly_the_opposite_book_to_momentum() -> None:
    data = _universe(
        {
            "AUSDT": [100.0] * 40 + [200.0] * 20,
            "BUSDT": [100.0] * 40 + [150.0] * 20,
            "CUSDT": [100.0] * 60,
            "DUSDT": [100.0] * 40 + [50.0] * 20,
        }
    )
    parameters = CrossSectionalParameters(
        lookback_hours=24, rebalance_hours=24, basket_size=1
    )
    momentum, reversion = MomentumStrategy(parameters), ReversionStrategy(parameters)
    momentum.prepare(data)
    reversion.prepare(data)
    context = PortfolioContext(T0 + 48 * HOUR, data, {}, 10_000.0)

    forward = momentum.decide(context)
    backward = reversion.decide(context)

    assert forward["AUSDT"] > 0 and backward["AUSDT"] < 0
    assert forward["DUSDT"] < 0 and backward["DUSDT"] > 0


def test_reversing_does_not_recover_costs() -> None:
    """Both directions pay the same toll; the two nets sum to -2 x cost."""
    data = dataset_of(
        history("AUSDT", [100.0, 100.0, 110.0, 110.0]),
        history("BUSDT", [100.0, 100.0, 90.0, 90.0]),
    )
    costs = CostModel(perp_taker_bps=Decimal("5.5"), slippage_bps=Decimal("0"))
    long_book = PortfolioEngine(data, costs).run(
        Fixed({"AUSDT": 1_000.0, "BUSDT": -1_000.0}), data.timeline
    )
    short_book = PortfolioEngine(data, costs).run(
        Fixed({"AUSDT": -1_000.0, "BUSDT": 1_000.0}), data.timeline
    )

    assert long_book.net_pnl + short_book.net_pnl == pytest.approx(
        -(long_book.total_costs + short_book.total_costs)
    )


def test_rebalance_minutes_overrides_the_hourly_cadence() -> None:
    """The 1m clock needs a sub-hourly cadence; hours cannot express it."""
    hourly = CrossSectionalParameters(rebalance_hours=6)
    sub_hourly = CrossSectionalParameters(rebalance_hours=6, rebalance_minutes=15)

    assert hourly.rebalance_ms == 6 * 3_600_000
    assert sub_hourly.rebalance_ms == 15 * 60_000


def test_strategy_rebalances_on_the_minute_schedule() -> None:
    data = _universe({name: [100.0] * 60 for name in ("AUSDT", "BUSDT", "CUSDT", "DUSDT")})
    strategy = MomentumStrategy(
        CrossSectionalParameters(lookback_hours=24, rebalance_minutes=15, basket_size=1)
    )
    strategy.prepare(data)
    held = {"AUSDT": 500.0}

    # The fixture is on an hourly grid, so every bar is a multiple of 15m.
    assert strategy._is_rebalance_bar(T0 + HOUR) is True
    assert strategy.decide(PortfolioContext(T0 + 30 * HOUR, data, held, 10_000.0)) != held


def test_momentum_refuses_a_stale_price_reference() -> None:
    item = history("AUSDT", [100.0] * 5)
    data = dataset_of(item)
    strategy = MomentumStrategy(CrossSectionalParameters(lookback_hours=24 * 30))
    strategy.prepare(data)

    # The lookback lands far before any stored bar, so no signal is produced.
    assert strategy.signal(data, "AUSDT", T0 + 2 * HOUR) is None
