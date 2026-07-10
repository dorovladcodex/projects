from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.models import (
    MarketSnapshot,
    PositionStatus,
    RiskDecision,
    Side,
    SignalAction,
    Symbol,
    TradeSignal,
)
from app.portfolio.paper_trading import PaperTradingService


def market(price: float = 60_000, symbol: Symbol = Symbol.BTCUSDT) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        last_price=price,
        bid_price=price - 1,
        ask_price=price + 1,
        trend_score=0.7,
        volatility_pct=2.0,
        liquidity_ok=True,
    )


def signal(side: Side = Side.BUY) -> TradeSignal:
    return TradeSignal(
        action=SignalAction.TRADE,
        symbol=Symbol.BTCUSDT,
        side=side,
        confidence=0.9,
        expected_edge_bps=20,
        stop_loss_pct=0.5,
    )


def approved_risk() -> RiskDecision:
    return RiskDecision(approved=True, max_loss_amount=50)


def test_opens_paper_position() -> None:
    service = PaperTradingService()

    position = service.open_from_signal(
        signal(),
        approved_risk(),
        market(),
        take_profit_pct=1.0,
    )

    assert position.status == PositionStatus.OPEN
    assert position.side == Side.BUY
    assert position.stop_loss < position.entry_price
    assert position.take_profit > position.entry_price
    assert service.open_position is not None


def test_blocks_second_position() -> None:
    service = PaperTradingService()
    service.open_from_signal(signal(), approved_risk(), market(), take_profit_pct=1.0)

    with pytest.raises(RuntimeError, match="maximum open paper positions"):
        service.open_from_signal(signal(), approved_risk(), market(), take_profit_pct=1.0)


def test_stop_loss_close() -> None:
    service = PaperTradingService()
    position = service.open_from_signal(signal(), approved_risk(), market(), take_profit_pct=1.0)

    closed = service.update_from_market(market(position.stop_loss - 1))

    assert closed is not None
    assert closed.status == PositionStatus.CLOSED
    assert closed.reason == "stop_loss"
    assert closed.realized_pnl < 0


def test_take_profit_close() -> None:
    service = PaperTradingService()
    position = service.open_from_signal(signal(), approved_risk(), market(), take_profit_pct=1.0)

    closed = service.update_from_market(market(position.take_profit + 1))

    assert closed is not None
    assert closed.status == PositionStatus.CLOSED
    assert closed.reason == "take_profit"
    assert closed.realized_pnl > 0


def test_timeout_close() -> None:
    service = PaperTradingService(timeout=timedelta(minutes=1))
    service.open_from_signal(signal(), approved_risk(), market(), take_profit_pct=10.0)

    closed = service.update_from_market(
        market(60_010),
        now=datetime.now(timezone.utc) + timedelta(minutes=2),
    )

    assert closed is not None
    assert closed.status == PositionStatus.CLOSED
    assert closed.reason == "timeout"


def test_manual_close_records_realized_pnl_and_closed_trade() -> None:
    service = PaperTradingService()
    position = service.open_from_signal(signal(), approved_risk(), market(), take_profit_pct=10.0)

    closed = service.close_position(position.entry_price + 100, reason="manual_close")

    assert closed.status == PositionStatus.CLOSED
    assert closed.reason == "manual_close"
    assert closed.realized_pnl > 0
    assert service.open_position is None
    assert service.positions_payload() == []
    assert service.trades_payload()[0]["reason"] == "manual_close"
    assert service.pnl().realized_pnl == pytest.approx(closed.realized_pnl)
    assert service.pnl().unrealized_pnl == 0


def test_close_rejects_unknown_reason() -> None:
    service = PaperTradingService()
    service.open_from_signal(signal(), approved_risk(), market(), take_profit_pct=10.0)

    with pytest.raises(ValueError, match="unsupported close reason"):
        service.close_position(60_100, reason="bad_reason")


def test_risk_manager_rejection_blocks_open() -> None:
    service = PaperTradingService()
    rejected = RiskDecision(approved=False, reasons=["expected edge too small"])

    with pytest.raises(PermissionError, match="risk manager rejected"):
        service.open_from_signal(signal(), rejected, market(), take_profit_pct=1.0)

    assert service.open_position is None


def test_pnl_calculation_for_short_position() -> None:
    service = PaperTradingService()
    position = service.open_from_signal(
        signal(Side.SELL),
        approved_risk(),
        market(),
        take_profit_pct=1.0,
    )

    service.update_from_market(market(position.entry_price - 100))

    pnl = service.pnl()
    assert pnl.unrealized_pnl > 0
    assert pnl.total_pnl == pytest.approx(pnl.unrealized_pnl)
