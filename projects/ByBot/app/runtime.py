from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.bybit.market_data import MarketDataService, snapshot_to_payload
from app.bybit.private import BybitAccountService, order_placement_blocked_reason
from app.config import BotMode, Settings
from app.models import CandidateLifecycleState, RiskContext, Symbol
from app.news.classifier import MockNewsClassifier
from app.news.service import NewsService
from app.portfolio.paper_trading import PaperTradingService
from app.risk import RiskManager, RiskRules
from app.signals.service import SignalCandidateService


def build_status(
    settings: Settings,
    market_data: MarketDataService,
    account_service: BybitAccountService,
    paper_trading: PaperTradingService,
    news_service: NewsService | None = None,
    signal_candidates: SignalCandidateService | None = None,
) -> dict[str, Any]:
    """Build a deterministic status snapshot.

    There is no real Bybit order placement here. Market data may be real public
    DATA_ONLY data or mock data depending on configuration.
    """

    news_service = news_service or NewsService(
        [], MockNewsClassifier(), max_item_age=timedelta(minutes=60)
    )
    market_payload = market_data.as_payload()
    account_status = account_service.status
    snapshots = market_data.latest_snapshots()

    signal = None
    market = None

    risk_context = RiskContext(
        equity=paper_trading.equity,
        available_balance=paper_trading.equity,
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
        max_position_notional_usdt=settings.max_position_notional_usdt,
        max_position_notional_pct_of_equity=settings.max_position_notional_pct_of_equity,
        min_position_notional_usdt=settings.min_position_notional_usdt,
        default_paper_fees_bps=settings.default_paper_fees_bps,
        default_slippage_bps=settings.default_slippage_bps,
        min_net_edge_bps=settings.min_net_edge_bps,
    )
    if (
        signal is not None
        and market is not None
        and market_data.status == "OK"
    ):
        risk_decision = RiskManager(risk_rules).assess(signal, market, risk_context)
        risk_reasons = risk_decision.reasons
        risk_approved = risk_decision.approved
        max_loss_amount = risk_decision.max_loss_amount
    else:
        risk_reasons = []
        if market_data.status != "OK":
            risk_reasons.append("market data unavailable")
        if not risk_reasons:
            risk_reasons.append("strategy did not produce a trade")
        risk_approved = False
        max_loss_amount = 0.0

    trading_blocked_data_unavailable = market_data.status != "OK"
    order_placement_blocked = True
    order_placement_blocked_reason_text = order_placement_blocked_reason(
        settings,
        account_status,
    )
    trading_enabled = settings.bot_mode in {BotMode.PAPER, BotMode.BYBIT_DEMO}
    trading_enabled = trading_enabled and not settings.trading_paused
    trading_enabled = trading_enabled and not trading_blocked_data_unavailable
    trading_enabled = trading_enabled and account_status.connected
    trading_enabled = trading_enabled and not order_placement_blocked
    risk_state = "OK" if risk_approved else "BLOCKED"
    btc_snapshot = market_data.latest_snapshot(Symbol.BTCUSDT)
    eth_snapshot = market_data.latest_snapshot(Symbol.ETHUSDT)
    paper_status = paper_trading.as_status()
    classifier_status = news_service.classifier_status_payload()
    classifier_metrics = news_service.classifier_metrics_payload()

    return {
        "name": settings.bot_name,
        "mode": settings.bot_mode.value,
        "live_trading": False,
        "trading_enabled": trading_enabled,
        "trading_paused": settings.trading_paused,
        "trading_blocked_data_unavailable": trading_blocked_data_unavailable,
        "private_api_connected": account_status.connected,
        "account_connection_status": "CONNECTED" if account_status.connected else "DISCONNECTED",
        "order_placement_blocked": order_placement_blocked,
        "order_placement_blocked_reason": order_placement_blocked_reason_text,
        "active_symbols": list(settings.allowed_symbols),
        "allowed_symbols": list(settings.allowed_symbols),
        "strategy": "NewsMomentumStrategy",
        "execution": "paper" if settings.bot_mode == BotMode.PAPER else "disabled",
        "open_paper_position": paper_status["open_position"],
        "last_signal": signal.model_dump(mode="json") if signal else None,
        "last_signal_candidate": (
            signal_candidates.last_result.candidate.model_dump(mode="json")
            if signal_candidates and signal_candidates.last_result else None
        ),
        "signal_candidates_count": len(signal_candidates.candidates) if signal_candidates else 0,
        "no_trade_candidates_count": signal_candidates.no_trade_candidates_count if signal_candidates else 0,
        "risk_preview_approved_count": (
            signal_candidates.risk_preview_approved_count if signal_candidates else 0
        ),
        "risk_preview_blocked_count": (
            signal_candidates.risk_preview_blocked_count if signal_candidates else 0
        ),
        "pending_signal_candidates_count": (
            signal_candidates.state_count(CandidateLifecycleState.PENDING_CONFIRMATION)
            if signal_candidates else 0
        ),
        "ready_signal_candidates_count": (
            signal_candidates.state_count(CandidateLifecycleState.READY)
            if signal_candidates else 0
        ),
        "blocked_signal_candidates_count": (
            signal_candidates.state_count(CandidateLifecycleState.BLOCKED)
            if signal_candidates else 0
        ),
        "expired_signal_candidates_count": (
            signal_candidates.state_count(CandidateLifecycleState.EXPIRED)
            if signal_candidates else 0
        ),
        "last_signal_evaluation_at": (
            signal_candidates.last_signal_evaluation_at.isoformat()
            if signal_candidates and signal_candidates.last_signal_evaluation_at else None
        ),
        "news_status": news_service.status,
        "last_news_item": news_service.last_news_item.model_dump(mode="json") if news_service.last_news_item else None,
        "last_filtered_news_item": (
            news_service.last_filtered_news_item.model_dump(mode="json")
            if news_service.last_filtered_news_item else None
        ),
        "last_news_classification": (
            news_service.last_news_classification.model_dump(mode="json")
            if news_service.last_news_classification else None
        ),
        "news_items_seen_count": news_service.items_seen_count,
        "news_items_filtered_count": news_service.items_filtered_count,
        "rss_items_seen": news_service.items_seen_count,
        "rss_items_accepted": news_service.items_filtered_count,
        "news_duplicates_skipped": news_service.news_duplicates_skipped,
        "news_skipped_before_codex_count": news_service.news_skipped_before_codex_count,
        "classifications_trade_eligible": news_service.classifications_trade_eligible,
        "signal_candidates_created": len(signal_candidates.candidates) if signal_candidates else 0,
        "items_classified_count": news_service.items_classified_count,
        "mock_classifier_calls_count": news_service.mock_classifier_calls_count,
        "news_classifier_mode": classifier_status.get("mode", "disabled"),
        "news_classifier_status": classifier_status.get("status", "DISABLED"),
        "real_llm_calls_count": classifier_metrics.get("real_llm_calls_count", 0),
        "successful_llm_calls_count": classifier_metrics.get("successful_llm_calls_count", 0),
        "failed_llm_calls_count": classifier_metrics.get("failed_llm_calls_count", 0),
        "llm_cache_hits": classifier_metrics.get("llm_cache_hits", 0),
        "llm_circuit_breaker_state": classifier_metrics.get(
            "llm_circuit_breaker_state", "DISABLED"
        ),
        "llm_requests_this_hour": classifier_metrics.get("llm_requests_this_hour", 0),
        "llm_requests_today": classifier_metrics.get("llm_requests_today", 0),
        "llm_input_tokens_today": classifier_metrics.get("llm_input_tokens_today", 0),
        "llm_output_tokens_today": classifier_metrics.get("llm_output_tokens_today", 0),
        "last_llm_error": classifier_metrics.get("last_llm_error"),
        "last_llm_call_at": classifier_metrics.get("last_llm_call_at"),
        "codex_cli_calls_count": classifier_metrics.get("codex_cli_calls_count", 0),
        "successful_codex_cli_calls_count": classifier_metrics.get(
            "successful_codex_cli_calls_count", 0
        ),
        "failed_codex_cli_calls_count": classifier_metrics.get(
            "failed_codex_cli_calls_count", 0
        ),
        "codex_cli_cache_hits": classifier_metrics.get("codex_cli_cache_hits", 0),
        "codex_cli_total_tokens_last_call": classifier_metrics.get(
            "codex_cli_total_tokens_last_call"
        ),
        "codex_cli_token_count_available": classifier_metrics.get(
            "codex_cli_token_count_available", False
        ),
        "codex_cli_total_tokens_today": classifier_metrics.get(
            "codex_cli_total_tokens_today", 0
        ),
        "codex_cli_requests_this_hour": classifier_metrics.get(
            "codex_cli_requests_this_hour", 0
        ),
        "codex_cli_requests_today": classifier_metrics.get("codex_cli_requests_today", 0),
        "last_codex_cli_duration_ms": classifier_metrics.get("last_codex_cli_duration_ms"),
        "last_codex_cli_error": classifier_metrics.get("last_codex_cli_error"),
        "last_codex_cli_call_at": classifier_metrics.get("last_codex_cli_call_at"),
        "estimated_input_tokens": news_service.estimated_input_tokens,
        "estimated_output_tokens": news_service.estimated_output_tokens,
        "news_last_error": news_service.last_error,
        "persistence_status": (
            "OK" if news_service.repository and news_service.repository.available
            else "UNAVAILABLE"
        ),
        "persistence_last_error": (
            news_service.repository.last_error if news_service.repository else "not configured"
        ),
        "market_data_status": market_data.status,
        "latest_btcusdt_snapshot": snapshot_to_payload(btc_snapshot) if btc_snapshot else None,
        "latest_ethusdt_snapshot": snapshot_to_payload(eth_snapshot) if eth_snapshot else None,
        "market": market_payload,
        "account": account_status.model_dump(mode="json"),
        "paper_trading": paper_status,
        "paper_trading_status": paper_status["status"],
        "paper_starting_equity_usdt": paper_trading.starting_equity,
        "paper_account_equity": paper_trading.equity,
        "paper_realized_pnl": paper_status["realized_pnl"],
        "paper_unrealized_pnl": paper_status["unrealized_pnl"],
        "last_paper_trade": paper_status["last_trade"],
        "last_risk_decision": paper_status["last_risk_decision"],
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
