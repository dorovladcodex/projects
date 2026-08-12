from __future__ import annotations

from decimal import Decimal

import pytest

from app.backtest.costs import CostModel, Liquidity
from app.backtest.data import Bar
from app.backtest.execution import MakerFills, TakerFills, build_fill_model
from app.backtest.portfolio import PortfolioContext, PortfolioEngine
from tests.test_backtest_engine import T0, HOUR, dataset_of, free
from tests.test_backtest_portfolio import Fixed


def bar(open_: float, high: float, low: float, close: float) -> Bar:
    return Bar(T0, open_, high, low, close)


# ------------------------------------------------------------------- taker


def test_taker_always_fills_at_the_open() -> None:
    model = TakerFills()
    fill = model.attempt(bar(100.0, 110.0, 90.0, 105.0), reference=None, buying=True)

    assert fill is not None
    assert fill.price == 100.0
    assert fill.liquidity is Liquidity.TAKER
    assert model.stats.fill_rate == 1.0


# ------------------------------------------------------------------- maker


def test_maker_buy_fills_only_when_price_comes_down_to_it() -> None:
    model = MakerFills()
    # Resting buy at 100; the bar traded down to 95, so it was reached.
    filled = model.attempt(bar(105.0, 110.0, 95.0, 108.0), reference=100.0, buying=True)
    assert filled is not None
    assert filled.price == 100.0
    assert filled.liquidity is Liquidity.MAKER


def test_maker_buy_is_missed_when_price_gaps_away() -> None:
    """Adverse selection: the trade you wanted most is the one you miss."""
    model = MakerFills()
    missed = model.attempt(bar(105.0, 120.0, 104.0, 118.0), reference=100.0, buying=True)

    assert missed is None
    assert model.stats.missed == 1
    assert model.stats.fill_rate == 0.0


def test_maker_sell_fills_only_when_price_rises_to_it() -> None:
    model = MakerFills()
    assert model.attempt(bar(95.0, 101.0, 90.0, 96.0), reference=100.0, buying=False) is not None
    assert model.attempt(bar(95.0, 99.0, 90.0, 96.0), reference=100.0, buying=False) is None


def test_maker_without_a_reference_cannot_rest_an_order() -> None:
    model = MakerFills()
    assert model.attempt(bar(100.0, 110.0, 90.0, 105.0), reference=None, buying=True) is None
    assert model.stats.missed == 1


def test_fill_rate_is_tracked_across_attempts() -> None:
    model = MakerFills()
    model.attempt(bar(105.0, 110.0, 95.0, 108.0), reference=100.0, buying=True)   # fills
    model.attempt(bar(105.0, 120.0, 104.0, 118.0), reference=100.0, buying=True)  # misses

    assert model.stats.requested == 2
    assert model.stats.filled == 1
    assert model.stats.fill_rate == pytest.approx(0.5)


def test_build_fill_model_selects_by_liquidity() -> None:
    assert isinstance(build_fill_model(Liquidity.MAKER), MakerFills)
    assert isinstance(build_fill_model(Liquidity.TAKER), TakerFills)


# --------------------------------------------------- engine integration


def ranged(symbol: str, rows: list[tuple[float, float, float, float]], start: int = T0):
    from app.backtest.data import SymbolHistory

    item = SymbolHistory(symbol=symbol)
    for offset, (open_, high, low, close) in enumerate(rows):
        item.perp.append(Bar(start + offset * HOUR, open_, high, low, close))
    item.build_index()
    return item


# A monotonic rally: every bar's low stays above the previous close, so a
# resting buy is never reached. This is the adverse-selection case that makes
# cheap maker fees misleading — the move you wanted is exactly the one missed.
RALLY = [
    (100.0, 100.0, 100.0, 100.0),
    (110.0, 115.0, 105.0, 112.0),
    (120.0, 125.0, 115.0, 122.0),
    (130.0, 135.0, 125.0, 132.0),
]


def test_maker_never_fills_into_a_rally_that_runs_away() -> None:
    data = dataset_of(ranged("AUSDT", RALLY))
    result = PortfolioEngine(data, free(), liquidity=Liquidity.MAKER).run(
        Fixed({"AUSDT": 1_000.0}), data.timeline
    )

    assert result.fills.filled == 0
    assert result.fills.missed >= 3
    assert result.total_turnover == pytest.approx(0.0)
    assert result.net_pnl == pytest.approx(0.0)


def test_maker_book_fills_when_the_bar_trades_through() -> None:
    data = dataset_of(
        ranged("AUSDT", [
            (100.0, 100.0, 100.0, 100.0),
            (101.0, 102.0, 98.0, 99.0),
            (99.0, 100.0, 98.0, 99.0),
        ])
    )
    result = PortfolioEngine(data, free(), liquidity=Liquidity.MAKER).run(
        Fixed({"AUSDT": 1_000.0}), data.timeline
    )

    assert result.fills.filled >= 1
    assert result.total_turnover > 0


def test_taker_captures_the_move_the_maker_book_missed_entirely() -> None:
    """Cheaper fees are worthless if the position never gets opened."""
    data = dataset_of(ranged("AUSDT", RALLY))
    taker = PortfolioEngine(data, free(), liquidity=Liquidity.TAKER).run(
        Fixed({"AUSDT": 1_000.0}), data.timeline
    )
    maker = PortfolioEngine(data, free(), liquidity=Liquidity.MAKER).run(
        Fixed({"AUSDT": 1_000.0}), data.timeline
    )

    assert taker.total_turnover > 0
    assert taker.net_pnl > 0
    assert maker.net_pnl == pytest.approx(0.0)


def test_maker_fee_is_cheaper_per_unit_traded() -> None:
    costs = CostModel(slippage_bps=Decimal("0"))
    assert costs.entry_bps("perp", Liquidity.MAKER) < costs.entry_bps("perp", Liquidity.TAKER)


def test_final_unwind_crosses_even_if_a_maker_order_would_miss() -> None:
    """The backtest must not end holding a position it never reported closing."""
    data = dataset_of(
        ranged("AUSDT", [
            (100.0, 100.0, 100.0, 100.0),
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 101.0, 99.0, 100.0),
            (200.0, 210.0, 199.0, 205.0),
        ])
    )
    result = PortfolioEngine(data, free(), liquidity=Liquidity.MAKER).run(
        Fixed({"AUSDT": 1_000.0}), data.timeline
    )

    assert result.episodes, "the open position must be closed and reported"
    assert result.episodes[-1].closed_ms == data.timeline[-1]


def test_fill_stats_are_attached_to_the_result() -> None:
    data = dataset_of(ranged("AUSDT", [(100.0, 101.0, 99.0, 100.0)] * 5))
    result = PortfolioEngine(data, free(), liquidity=Liquidity.MAKER).run(
        Fixed({"AUSDT": 1_000.0}), data.timeline
    )

    assert result.fills.requested > 0
    assert result.fills.filled + result.fills.missed == result.fills.requested
