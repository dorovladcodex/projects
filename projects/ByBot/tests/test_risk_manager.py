from datetime import datetime, timezone

from app.models import (
    MarketSnapshot,
    RiskContext,
    Side,
    SignalAction,
    Symbol,
    TradeSignal,
)
from app.risk import RiskManager


def valid_signal() -> TradeSignal:
    return TradeSignal(
        action=SignalAction.TRADE,
        symbol=Symbol.BTCUSDT,
        side=Side.BUY,
        confidence=0.9,
        expected_edge_bps=20,
        stop_loss_pct=0.5,
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
