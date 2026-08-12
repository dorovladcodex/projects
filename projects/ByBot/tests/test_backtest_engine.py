from __future__ import annotations

from decimal import Decimal

import pytest

from app.backtest.costs import CostModel, Liquidity, stressed
from app.backtest.data import Bar, Dataset, SymbolHistory, coverage_report
from app.backtest.engine import BacktestEngine, BarContext, CarryPosition
from app.backtest.metrics import evaluate, promotion_gates
from app.backtest.strategies import CarryParameters, FundingCarryStrategy
from app.backtest.validation import chronological_folds, split_holdout

HOUR = 3_600_000
T0 = 1_700_000_000_000 // HOUR * HOUR


def history(
    symbol: str,
    prices: list[float],
    *,
    spot: list[float] | None = None,
    funding: dict[int, str] | None = None,
    start: int = T0,
) -> SymbolHistory:
    spot_prices = spot if spot is not None else prices
    item = SymbolHistory(symbol=symbol)
    for offset, price in enumerate(prices):
        stamp = start + offset * HOUR
        item.perp.append(Bar(stamp, price, price, price, price))
    for offset, price in enumerate(spot_prices):
        stamp = start + offset * HOUR
        item.spot[stamp] = Bar(stamp, price, price, price, price)
    item.funding = {k: Decimal(v) for k, v in (funding or {}).items()}
    item.build_index()
    return item


def dataset_of(*items: SymbolHistory) -> Dataset:
    data = Dataset(symbols={item.symbol: item for item in items})
    stamps: set[int] = set()
    for item in items:
        stamps.update(bar.start_ms for bar in item.perp)
    data.timeline = sorted(stamps)
    return data


class Scripted:
    """Strategy that replays a fixed decision per timestamp."""

    def __init__(self, script: dict[int, dict[str, float]]) -> None:
        self.script = script
        self.seen: list[BarContext] = []

    def prepare(self, dataset: Dataset) -> None:
        return None

    def decide(self, context: BarContext) -> dict[str, float]:
        self.seen.append(context)
        return self.script.get(context.timestamp_ms, {})


def free() -> CostModel:
    return CostModel(
        perp_taker_bps=Decimal("0"), perp_maker_bps=Decimal("0"),
        spot_taker_bps=Decimal("0"), spot_maker_bps=Decimal("0"),
        slippage_bps=Decimal("0"),
    )


# ------------------------------------------------------------- no lookahead


def test_decision_fills_at_the_next_bar_open_not_the_current_close() -> None:
    """The core anti-lookahead guarantee."""
    data = dataset_of(history("BTCUSDT", [100.0, 200.0, 200.0], spot=[100.0, 200.0, 200.0]))
    engine = BacktestEngine(data, free())
    # Decide at bar 0; the fill must use bar 1's open (200), not bar 0's (100).
    result = engine.run(Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline)

    assert result.trades[0].opened_ms == T0 + HOUR
    assert result.trades[0].price_pnl == pytest.approx(0.0)


def test_strategy_never_sees_a_timestamp_beyond_the_current_bar() -> None:
    data = dataset_of(history("BTCUSDT", [100.0] * 5))
    strategy = Scripted({})
    BacktestEngine(data, free()).run(strategy, data.timeline)

    assert [context.timestamp_ms for context in strategy.seen] == data.timeline
    for context in strategy.seen:
        assert context.timestamp_ms <= data.timeline[-1]


def test_open_symbols_reflect_state_before_the_new_decision() -> None:
    data = dataset_of(history("BTCUSDT", [100.0] * 4))
    strategy = Scripted({T0: {"BTCUSDT": 500.0}})
    BacktestEngine(data, free()).run(strategy, data.timeline)

    assert strategy.seen[0].open_symbols == frozenset()
    assert strategy.seen[2].open_symbols == frozenset({"BTCUSDT"})


# ------------------------------------------------------------------ funding


def test_short_perp_receives_positive_funding() -> None:
    data = dataset_of(
        history("BTCUSDT", [100.0] * 4, funding={T0 + 2 * HOUR: "0.0001"})
    )
    result = BacktestEngine(data, free()).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline
    )

    assert result.funding_events == 1
    assert result.trades[0].funding_pnl == pytest.approx(0.1)  # 1000 * 0.0001


def test_negative_funding_is_a_cost_to_the_carry_position() -> None:
    data = dataset_of(
        history("BTCUSDT", [100.0] * 4, funding={T0 + 2 * HOUR: "-0.0005"})
    )
    result = BacktestEngine(data, free()).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline
    )

    assert result.trades[0].funding_pnl == pytest.approx(-0.5)


def test_funding_only_accrues_while_the_position_is_open() -> None:
    data = dataset_of(
        history("BTCUSDT", [100.0] * 5, funding={T0: "0.001", T0 + 3 * HOUR: "0.001"})
    )
    # Open at bar 1 (decided at bar 0), so the settlement at T0 must be missed.
    result = BacktestEngine(data, free()).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline
    )

    assert result.funding_events == 1
    assert result.trades[0].funding_pnl == pytest.approx(1.0)


# -------------------------------------------------------------------- costs


def test_both_legs_are_charged_on_entry_and_exit() -> None:
    data = dataset_of(history("BTCUSDT", [100.0] * 4))
    costs = CostModel(
        perp_taker_bps=Decimal("5.5"), spot_taker_bps=Decimal("10.0"),
        slippage_bps=Decimal("0"),
    )
    result = BacktestEngine(data, costs).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}, T0 + 2 * HOUR: {}}), data.timeline
    )

    # (5.5 + 10) in + (5.5 + 10) out = 31 bps of 1000 = 3.1
    assert result.trades[0].costs == pytest.approx(3.1)


def test_carry_round_trip_matches_the_measured_perp_cost() -> None:
    costs = CostModel(slippage_bps=Decimal("0"))
    assert costs.round_trip_bps("perp", Liquidity.TAKER) == Decimal("11.0")
    assert costs.carry_round_trip_bps(Liquidity.TAKER) == Decimal("31.0")


def test_spot_fees_are_not_assumed_cheaper_than_perp() -> None:
    costs = CostModel()
    assert costs.spot_taker_bps > costs.perp_taker_bps


def test_breakeven_days_uses_the_full_two_leg_hurdle() -> None:
    costs = CostModel(slippage_bps=Decimal("0"))
    days = costs.breakeven_days(Decimal("3.326"), Liquidity.TAKER)
    assert days == pytest.approx(Decimal("31.0") / Decimal("3.326"), rel=1e-6)


def test_breakeven_is_undefined_for_non_positive_funding() -> None:
    assert CostModel().breakeven_days(Decimal("0")) is None


def test_cost_stress_scales_every_component() -> None:
    doubled = stressed(CostModel(), Decimal("2"))
    assert doubled.perp_taker_bps == Decimal("11.0")
    assert doubled.spot_taker_bps == Decimal("20.0")
    assert doubled.slippage_bps == Decimal("2.0")


def test_entry_cost_reduces_equity_immediately() -> None:
    data = dataset_of(history("BTCUSDT", [100.0] * 4))
    costs = CostModel(slippage_bps=Decimal("0"))
    result = BacktestEngine(data, costs, starting_equity=10_000.0).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline
    )

    # Bar 2 is after the fill; equity must already carry the entry cost.
    assert result.equity_curve[2][1] < 10_000.0


# --------------------------------------------------- missing data integrity


def test_missing_spot_leg_skips_the_trade_instead_of_inventing_a_price() -> None:
    item = history("BTCUSDT", [100.0] * 4)
    item.spot.pop(T0 + HOUR)
    data = dataset_of(item)
    result = BacktestEngine(data, free()).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline
    )

    assert result.skipped_missing_leg == 1
    assert result.trades == []


def test_symbol_is_not_tradeable_before_it_lists() -> None:
    late = history("WIFUSDT", [1.0] * 3, start=T0 + 10 * HOUR)
    data = dataset_of(history("BTCUSDT", [100.0] * 20), late)

    assert data.tradeable("WIFUSDT", T0, require_spot=True) is False
    assert data.tradeable("WIFUSDT", T0 + 10 * HOUR, require_spot=True) is True


def test_unknown_symbol_is_never_tradeable() -> None:
    data = dataset_of(history("BTCUSDT", [100.0] * 3))
    assert data.tradeable("NOPEUSDT", T0, require_spot=False) is False


def test_coverage_report_exposes_missing_spot() -> None:
    item = history("BTCUSDT", [100.0] * 10)
    for offset in range(5):
        item.spot.pop(T0 + offset * HOUR)
    rows = coverage_report(dataset_of(item))

    assert rows[0].both_legs == 5
    assert rows[0].spot_coverage_pct == pytest.approx(50.0)


# --------------------------------------------------------------- accounting


def test_delta_neutral_pair_is_flat_when_both_legs_move_together() -> None:
    data = dataset_of(
        history("BTCUSDT", [100.0, 100.0, 150.0, 150.0], spot=[100.0, 100.0, 150.0, 150.0])
    )
    result = BacktestEngine(data, free()).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline
    )

    assert result.trades[0].price_pnl == pytest.approx(0.0, abs=1e-9)


def test_basis_widening_against_the_pair_is_a_loss() -> None:
    # Perp rises faster than spot: short perp loses more than long spot gains.
    data = dataset_of(
        history("BTCUSDT", [100.0, 100.0, 110.0, 110.0], spot=[100.0, 100.0, 105.0, 105.0])
    )
    result = BacktestEngine(data, free()).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline
    )

    assert result.trades[0].price_pnl < 0


def test_position_unrealized_matches_manual_calculation() -> None:
    position = CarryPosition("BTCUSDT", 1_000.0, T0, perp_entry=100.0, spot_entry=100.0)
    # perp -10%, spot -5%: short perp +100, long spot -50
    assert position.unrealized(90.0, 95.0) == pytest.approx(50.0)


def test_open_position_is_closed_on_the_final_bar() -> None:
    data = dataset_of(history("BTCUSDT", [100.0] * 4))
    result = BacktestEngine(data, free()).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline
    )

    assert len(result.trades) == 1
    assert result.trades[0].closed_ms == data.timeline[-1]


# ------------------------------------------------------------------ metrics


def test_metrics_summarise_a_winning_and_a_losing_trade() -> None:
    data = dataset_of(
        history("BTCUSDT", [100.0] * 6, funding={T0 + 2 * HOUR: "0.001"})
    )
    result = BacktestEngine(data, free()).run(
        Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline
    )
    metrics = evaluate(result)

    assert metrics.trades == 1
    assert metrics.wins == 1
    assert metrics.funding_pnl == pytest.approx(1.0)
    assert metrics.expectancy_bps == pytest.approx(10.0)


def test_profit_factor_is_infinite_without_losses() -> None:
    data = dataset_of(history("BTCUSDT", [100.0] * 4, funding={T0 + 2 * HOUR: "0.001"}))
    metrics = evaluate(
        BacktestEngine(data, free()).run(Scripted({T0: {"BTCUSDT": 1_000.0}}), data.timeline)
    )
    assert metrics.profit_factor == float("inf")


def test_empty_result_produces_zeroed_metrics() -> None:
    data = dataset_of(history("BTCUSDT", [100.0] * 4))
    metrics = evaluate(BacktestEngine(data, free()).run(Scripted({}), data.timeline))

    assert metrics.trades == 0
    assert metrics.expectancy_bps == 0.0
    assert metrics.win_rate == 0.0


# ----------------------------------------------------------------- gates


def test_gates_fail_when_folds_are_negative() -> None:
    losing = evaluate(
        BacktestEngine(
            dataset_of(history("BTCUSDT", [100.0] * 4, funding={T0 + 2 * HOUR: "-0.001"})),
            free(),
        ).run(Scripted({T0: {"BTCUSDT": 1_000.0}}), [T0 + i * HOUR for i in range(4)])
    )
    gates = promotion_gates([losing], None)

    assert not all(gate.passed for gate in gates)
    assert any(gate.name == "oos_expectancy_positive" and not gate.passed for gate in gates)


def test_gates_require_a_minimum_trade_count() -> None:
    winning = evaluate(
        BacktestEngine(
            dataset_of(history("BTCUSDT", [100.0] * 4, funding={T0 + 2 * HOUR: "0.001"})),
            free(),
        ).run(Scripted({T0: {"BTCUSDT": 1_000.0}}), [T0 + i * HOUR for i in range(4)])
    )
    gates = {gate.name: gate for gate in promotion_gates([winning], None)}

    assert gates["minimum_30_oos_trades"].passed is False


# ------------------------------------------------------------- validation


def test_folds_are_chronological_and_never_overlap_their_training_window() -> None:
    timeline = [T0 + i * HOUR for i in range(100)]
    folds = chronological_folds(timeline, 4)

    assert len(folds) == 4
    for fold in folds:
        assert fold.train_to_ms < fold.test_from_ms
    for earlier, later in zip(folds, folds[1:]):
        assert earlier.test_from_ms < later.test_from_ms


def test_holdout_is_the_final_slice() -> None:
    timeline = [T0 + i * HOUR for i in range(100)]
    development, holdout = split_holdout(timeline, 0.2)

    assert len(holdout) == 20
    assert development[-1] < holdout[0]
    assert development + holdout == timeline


def test_too_short_a_timeline_yields_no_folds() -> None:
    assert chronological_folds([T0, T0 + HOUR], 4) == []


# ------------------------------------------------------------ carry signal


def test_funding_window_excludes_settlements_after_the_decision_point() -> None:
    item = history(
        "BTCUSDT", [100.0] * 10,
        funding={T0: "0.001", T0 + 5 * HOUR: "0.001", T0 + 9 * HOUR: "999"},
    )
    strategy = FundingCarryStrategy(CarryParameters(lookback_hours=24, min_settlements=1))
    strategy.prepare(dataset_of(item))

    rate, count = strategy.funding_bps_per_day("BTCUSDT", T0 + 6 * HOUR)

    assert count == 2  # the far-future 999 settlement must not be visible
    assert rate < 100


def test_carry_requires_a_minimum_number_of_settlements() -> None:
    item = history("BTCUSDT", [100.0] * 10, funding={T0: "0.01"})
    data = dataset_of(item)
    strategy = FundingCarryStrategy(
        CarryParameters(min_settlements=5, lookback_hours=24, entry_bps_per_day=0.0)
    )
    strategy.prepare(data)

    decision = strategy.decide(
        BarContext(T0 + 5 * HOUR, data, frozenset(), 10_000.0)
    )
    assert decision == {}


def test_carry_uses_hysteresis_to_avoid_churn() -> None:
    item = history(
        "BTCUSDT", [100.0] * 30,
        funding={T0 + i * HOUR: "0.00002" for i in range(0, 24, 8)},
    )
    data = dataset_of(item)
    parameters = CarryParameters(
        lookback_hours=24, entry_bps_per_day=100.0, exit_bps_per_day=0.0, min_settlements=1
    )
    strategy = FundingCarryStrategy(parameters)
    strategy.prepare(data)

    stamp = T0 + 20 * HOUR
    assert strategy.decide(BarContext(stamp, data, frozenset(), 10_000.0)) == {}
    held = strategy.decide(BarContext(stamp, data, frozenset({"BTCUSDT"}), 10_000.0))
    assert "BTCUSDT" in held


def test_carry_respects_max_positions() -> None:
    items = [
        history(name, [100.0] * 30, funding={T0 + i * HOUR: "0.001" for i in range(0, 24, 8)})
        for name in ("AUSDT", "BUSDT", "CUSDT", "DUSDT")
    ]
    data = dataset_of(*items)
    strategy = FundingCarryStrategy(
        CarryParameters(lookback_hours=24, entry_bps_per_day=0.0, max_positions=2, min_settlements=1)
    )
    strategy.prepare(data)

    decision = strategy.decide(BarContext(T0 + 20 * HOUR, data, frozenset(), 10_000.0))
    assert len(decision) == 2
