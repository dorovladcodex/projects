from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db.persistence import PersistenceRepository
from app.models import (
    CandidateLifecycleState,
    MarketConfirmation,
    MarketSnapshot,
    NewsSignalAction,
    NewsSignalCandidate,
    Sentiment,
    Side,
    SignalRiskPreview,
    SimpleTrend,
    Symbol,
)
from app.portfolio.paper_trading import PaperTradingService


def candidate(symbol: Symbol, *, side: Side = Side.BUY) -> NewsSignalCandidate:
    action = NewsSignalAction.BUY if side == Side.BUY else NewsSignalAction.SELL
    return NewsSignalCandidate(
        news_id="00000000-0000-0000-0000-000000000001",
        symbol=symbol,
        state=CandidateLifecycleState.READY,
        proposed_action=action,
        final_action=action,
        sentiment=Sentiment.BULLISH if side == Side.BUY else Sentiment.BEARISH,
        classification_confidence=0.95,
        news_importance=0.95,
        category="etf",
        urgency="high",
        market_confirmation=MarketConfirmation(
            available=True, fresh=True, direction_confirmed=True
        ),
        expected_edge_bps=30,
        proposed_stop_loss_pct=1,
        proposed_take_profit_pct=2,
        ttl_seconds=300,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def preview() -> SignalRiskPreview:
    return SignalRiskPreview(
        preview_performed=True,
        approved=True,
        capped_size=1,
        position_notional=100,
        max_allowed_notional=500,
        risk_decision_id=1,
    )


def snapshot(symbol: Symbol, price: float = 100) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        last_price=price,
        bid_price=price - 0.01,
        ask_price=price + 0.01,
        price_change_1m_pct=0.5,
        simple_trend=SimpleTrend.BULLISH,
        trend_score=1,
        volatility_pct=0.1,
        liquidity_ok=True,
    )


def open_candidate(
    service: PaperTradingService, symbol: Symbol, *, side: Side = Side.BUY
):
    return service.open_from_candidate(
        candidate(symbol, side=side),
        preview(),
        snapshot(symbol),
        taker_fee_bps=6,
        slippage_bps=2,
    )


def test_one_position_per_symbol_and_configurable_total_limit() -> None:
    service = PaperTradingService(max_total_open_positions=2)
    btc = open_candidate(service, Symbol.BTCUSDT)
    duplicate_btc = open_candidate(service, Symbol.BTCUSDT)

    assert btc is not None
    assert duplicate_btc is None
    assert "per symbol" in (service.last_error or "")
    eth = open_candidate(service, Symbol.ETHUSDT)
    assert eth is not None
    assert len(service.open_positions) == 2

    limited = PaperTradingService(max_total_open_positions=1)
    assert open_candidate(limited, Symbol.BTCUSDT) is not None
    assert open_candidate(limited, Symbol.ETHUSDT) is None
    assert "maximum total" in (limited.last_error or "")


def test_symbol_and_global_cooldowns_expire_without_sleeping() -> None:
    now = datetime.now(timezone.utc)
    service = PaperTradingService(
        max_total_open_positions=2,
        symbol_cooldown=timedelta(minutes=5),
        global_entry_cooldown=timedelta(minutes=1),
    )
    opened = open_candidate(service, Symbol.BTCUSDT)
    assert opened is not None
    assert any(
        "global paper entry cooldown" in reason
        for reason in service.entry_block_reasons(Symbol.ETHUSDT, now=now)
    )

    service.last_entry_at = now - timedelta(minutes=2)
    service.close_position(
        opened.entry_price, reason="manual_close", now=now,
        position_id=opened.id,
    )
    assert "symbol cooldown" in " ".join(
        service.entry_block_reasons(Symbol.BTCUSDT, now=now)
    )
    service.symbol_cooldown_until[Symbol.BTCUSDT] = now - timedelta(seconds=1)
    assert not any(
        "symbol cooldown" in reason
        for reason in service.entry_block_reasons(Symbol.BTCUSDT, now=now)
    )


@pytest.mark.parametrize(
    "daily_limit,weekly_limit,drawdown_limit,expected_reason",
    [
        (0.01, 100, 100, "maximum daily net loss reached"),
        (100, 0.01, 100, "maximum weekly net loss reached"),
    ],
)
def test_realized_loss_triggers_latched_kill_switch(
    daily_limit: float,
    weekly_limit: float,
    drawdown_limit: float,
    expected_reason: str,
) -> None:
    service = PaperTradingService(
        max_daily_net_loss_pct=daily_limit,
        max_weekly_net_loss_pct=weekly_limit,
        max_account_drawdown_pct=drawdown_limit,
    )
    opened = open_candidate(service, Symbol.BTCUSDT)
    assert opened is not None
    service.close_position(90, reason="stop_loss", position_id=opened.id)

    assert service.kill_switch_active is True
    assert expected_reason in service.kill_switch_reasons
    assert service.entry_block_reasons(Symbol.ETHUSDT)


def test_drawdown_kill_switch_blocks_entries_but_open_position_can_close() -> None:
    service = PaperTradingService(
        max_total_open_positions=2,
        max_daily_net_loss_pct=100,
        max_weekly_net_loss_pct=100,
        max_account_drawdown_pct=0.005,
    )
    opened = open_candidate(service, Symbol.BTCUSDT)
    assert opened is not None
    service.update_from_market(snapshot(Symbol.BTCUSDT, 99.5))

    assert service.kill_switch_active is True
    assert "drawdown" in " ".join(service.kill_switch_reasons)
    assert open_candidate(service, Symbol.ETHUSDT) is None

    closed = service.close_position(
        99.5, reason="manual_close", position_id=opened.id
    )
    assert closed.status.value == "CLOSED"
    assert service.open_positions == []
    assert service.kill_switch_active is True


def test_restart_restores_kill_switch_and_expires_stale_cooldowns(tmp_path) -> None:
    url = f"sqlite:///{tmp_path / 'risk-state.db'}"
    repository = PersistenceRepository(url)
    service = PaperTradingService(
        repository=repository,
        symbol_cooldown=timedelta(minutes=5),
        global_entry_cooldown=timedelta(minutes=5),
    )
    service.restore()
    now = datetime.now(timezone.utc)
    service.kill_switch_active = True
    service.kill_switch_reasons = ["test persisted kill switch"]
    service.last_entry_at = now - timedelta(minutes=10)
    service.symbol_cooldown_until[Symbol.BTCUSDT] = now - timedelta(seconds=1)
    service._persist_risk_state(now)

    restarted = PaperTradingService(
        repository=PersistenceRepository(url),
        symbol_cooldown=timedelta(minutes=5),
        global_entry_cooldown=timedelta(minutes=5),
    )
    restarted.restore()

    assert restarted.kill_switch_active is True
    assert restarted.kill_switch_reasons == ["test persisted kill switch"]
    assert Symbol.BTCUSDT not in restarted.symbol_cooldown_until
    assert restarted.risk_status()["entries_allowed"] is False


def test_paper_execution_has_no_bybit_order_adapter() -> None:
    service = PaperTradingService()
    assert not hasattr(service, "bybit_order_adapter")
    assert open_candidate(service, Symbol.BTCUSDT) is not None
