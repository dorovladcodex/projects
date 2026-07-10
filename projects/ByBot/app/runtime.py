from __future__ import annotations

from typing import Any

from app.bybit.market_data import MarketDataService
from app.config import BotMode, Settings
from app.models import RiskContext
from app.news.classifier import MockNewsClassifier
from app.news.mock import mock_news_item
from app.risk import RiskManager, RiskRules
from app.strategy import NewsMomentumStrategy


def build_status(settings: Settings, market_data: MarketDataService) -> dict[str, Any]:
    """Build a deterministic status snapshot.

    There is no real Bybit order placement here. Market data may be real public
    DATA_ONLY data or mock data depending on configuration.
    """

    market_payload = market_data.as_payload()
    snapshots = market_data.latest_snapshots()

    news = mock_news_item()
    classification = MockNewsClassifier().classify(news)
    market = snapshots[0] if snapshots else None
    signal = (
        NewsMomentumStrategy().evaluate(news, classification, market)
        if market is not None
        else None
    )

    risk_context = RiskContext(
        equity=settings.paper_starting_equity,
        requested_risk_pct=settings.max_risk_per_trade_pct,
        leverage=settings.max_leverage,
        open_positions=0,
        daily_pnl_pct=settings.paper_daily_pnl_pct,
        weekly_pnl_pct=settings.paper_weekly_pnl_pct,
        consecutive_losses=settings.paper_consecutive_losses,
        api_stable=market_data.status == "OK",
    )
    risk_rules = RiskRules(
        max_risk_per_trade_pct=settings.max_risk_per_trade_pct,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_weekly_loss_pct=settings.max_weekly_loss_pct,
        max_leverage=settings.max_leverage,
        max_spread_bps=settings.max_spread_bps,
        min_confidence=settings.min_llm_confidence,
        min_expected_edge_bps=settings.min_expected_edge_bps,
    )
    if signal is not None and market is not None and market_data.status == "OK":
        risk_decision = RiskManager(risk_rules).assess(signal, market, risk_context)
        risk_reasons = risk_decision.reasons
        risk_approved = risk_decision.approved
        max_loss_amount = risk_decision.max_loss_amount
    else:
        risk_reasons = ["market data unavailable"]
        risk_approved = False
        max_loss_amount = 0.0

    trading_enabled = settings.bot_mode in {BotMode.PAPER, BotMode.BYBIT_DEMO}
    trading_enabled = trading_enabled and not settings.trading_paused
    risk_state = "OK" if risk_approved else "BLOCKED"

    return {
        "name": settings.bot_name,
        "mode": settings.bot_mode.value,
        "live_trading": False,
        "trading_enabled": trading_enabled,
        "trading_paused": settings.trading_paused,
        "active_symbols": list(settings.allowed_symbols),
        "allowed_symbols": list(settings.allowed_symbols),
        "strategy": "NewsMomentumStrategy",
        "execution": "paper" if settings.bot_mode == BotMode.PAPER else "disabled",
        "open_paper_position": None,
        "last_signal": signal.model_dump(mode="json") if signal else None,
        "market": market_payload,
        "risk_status": {
            "state": risk_state,
            "approved": risk_approved,
            "reasons": risk_reasons,
            "max_loss_amount": max_loss_amount,
            "limits": {
                "max_risk_per_trade_pct": settings.max_risk_per_trade_pct,
                "max_daily_loss_pct": settings.max_daily_loss_pct,
                "max_weekly_loss_pct": settings.max_weekly_loss_pct,
                "max_leverage": settings.max_leverage,
            },
        },
    }
