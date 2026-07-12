from datetime import datetime, timezone

from app.models import (
    MarketSnapshot,
    RiskContext,
    Side,
    SignalAction,
    Symbol,
    TradeSignal,
)
from app.risk import RiskManager, RiskRules


def valid_signal() -> TradeSignal:
    return TradeSignal(
        action=SignalAction.TRADE,
        symbol=Symbol.BTCUSDT,
        side=Side.BUY,
        confidence=0.9,
        expected_edge_bps=20,
        stop_loss_pct=0.5,
        take_profit_pct=1.0,
    )


def valid_market() -> MarketSnapshot:
    return MarketSnapshot(
        symbol=Symbol.BTCUSDT,
        timestamp=datetime.now(timezone.utc),
        last_price=60_000,
        bid_price=59_999,
        ask_price=60_001,
        trend_score=0.7,
        volatility_pct=2.0,
        liquidity_ok=True,
    )


def valid_context() -> RiskContext:
    return RiskContext(
        equity=10_000,
        requested_risk_pct=0.5,
        leverage=2,
        open_positions=0,
        daily_pnl_pct=0,
        weekly_pnl_pct=0,
        consecutive_losses=0,
    )


def test_risk_manager_approves_valid_trade() -> None:
    decision = RiskManager().assess(valid_signal(), valid_market(), valid_context())

    assert decision.approved is True
    assert decision.max_loss_amount == 50
    assert decision.position_notional <= decision.max_allowed_notional


def test_risk_manager_blocks_second_position() -> None:
    context = valid_context()
    context.open_positions = 1

    decision = RiskManager().assess(valid_signal(), valid_market(), context)

    assert decision.approved is False
    assert "maximum open positions reached" in decision.reasons


def test_risk_manager_blocks_after_three_losses() -> None:
    context = valid_context()
    context.consecutive_losses = 3

    decision = RiskManager().assess(valid_signal(), valid_market(), context)

    assert decision.approved is False
    assert "paused after consecutive losses" in decision.reasons


def test_risk_manager_requires_stop_loss() -> None:
    signal = valid_signal()
    signal.stop_loss_pct = None

    decision = RiskManager().assess(signal, valid_market(), valid_context())

    assert decision.approved is False
    assert "stop loss is mandatory" in decision.reasons


def test_tight_stop_does_not_create_unrealistic_huge_position() -> None:
    signal = valid_signal()
    signal.stop_loss_pct = 0.01
    context = valid_context()
    context.requested_risk_pct = 0.05
    context.leverage = 1
    decision = RiskManager(
        RiskRules(max_position_notional_usdt=5_000, max_position_notional_pct_of_equity=5)
    ).assess(signal, valid_market(), context)

    assert decision.approved is True
    assert decision.risk_based_size > decision.capped_size
    assert decision.size_was_capped is True
    assert decision.position_notional <= 500
    assert decision.capped_size < 0.01


def test_notional_is_capped_by_max_position_notional_usdt() -> None:
    decision = RiskManager(
        RiskRules(max_position_notional_usdt=250, max_position_notional_pct_of_equity=100)
    ).assess(valid_signal(), valid_market(), valid_context())

    assert decision.approved is True
    assert decision.position_notional <= 250


def test_notional_is_capped_by_equity_times_leverage() -> None:
    context = valid_context()
    context.equity = 100
    context.leverage = 1
    decision = RiskManager(
        RiskRules(max_position_notional_usdt=5_000, max_position_notional_pct_of_equity=100)
    ).assess(valid_signal(), valid_market(), context)

    assert decision.approved is True
    assert decision.position_notional <= 100


def test_notional_is_capped_by_available_balance_times_leverage() -> None:
    context = valid_context()
    context.available_balance = 50
    context.leverage = 2
    decision = RiskManager(
        RiskRules(max_position_notional_usdt=5_000, max_position_notional_pct_of_equity=100)
    ).assess(valid_signal(), valid_market(), context)

    assert decision.approved is True
    assert decision.position_notional <= 100


def test_trade_rejected_if_expected_edge_after_fees_slippage_is_too_small() -> None:
    signal = valid_signal()
    signal.expected_edge_bps = 10
    decision = RiskManager(
        RiskRules(default_paper_fees_bps=6, default_slippage_bps=2)
    ).assess(signal, valid_market(), valid_context())

    assert decision.approved is False
    assert "expected edge after fees and slippage is too small" in decision.reasons


def test_take_profit_smaller_than_costs_is_rejected() -> None:
    signal = valid_signal()
    signal.take_profit_pct = 0.01
    signal.expected_edge_bps = 0.5
    decision = RiskManager(
        RiskRules(default_paper_fees_bps=6, default_slippage_bps=2, min_net_edge_bps=5)
    ).assess(signal, valid_market(), valid_context())

    assert decision.approved is False
    assert "take profit is smaller than fees/slippage plus required net edge" in decision.reasons
    assert decision.take_profit_bps == 1
    assert decision.round_trip_cost_bps == 16


def test_take_profit_above_costs_but_below_min_net_edge_is_rejected() -> None:
    signal = valid_signal()
    signal.take_profit_pct = 0.20
    signal.expected_edge_bps = 15
    decision = RiskManager(
        RiskRules(default_paper_fees_bps=6, default_slippage_bps=2, min_net_edge_bps=5)
    ).assess(signal, valid_market(), valid_context())

    assert decision.approved is False
    assert "take profit is smaller than fees/slippage plus required net edge" in decision.reasons


def test_expected_edge_greater_than_take_profit_is_rejected() -> None:
    signal = valid_signal()
    signal.take_profit_pct = 0.25
    signal.expected_edge_bps = 30
    decision = RiskManager().assess(signal, valid_market(), valid_context())

    assert decision.approved is False
    assert "expected edge is inconsistent with take profit target" in decision.reasons


def test_valid_take_profit_after_costs_is_accepted() -> None:
    signal = valid_signal()
    signal.take_profit_pct = 0.5
    signal.expected_edge_bps = 20
    decision = RiskManager(
        RiskRules(default_paper_fees_bps=6, default_slippage_bps=2, min_net_edge_bps=5)
    ).assess(signal, valid_market(), valid_context())

    assert decision.approved is True
    assert decision.take_profit_bps == 50
    assert decision.expected_net_edge_bps == 4
