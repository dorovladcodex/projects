from __future__ import annotations

from collections.abc import AsyncIterator
import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import FastAPI, HTTPException

from app.bybit.market_data import build_market_data_service, snapshot_to_payload
from app.bybit.private import build_account_service
from app.config import BotMode, get_settings
from app.models import (
    MarketSnapshot,
    ClassifierTestRequest,
    NewsItem,
    PaperTestSignalRequest,
    PaperMarketSnapshotTestRequest,
    RiskContext,
    SignalAction,
    SignalTestFromNewsRequest,
    Symbol,
    TestMarketSnapshotRequest,
    TradeSignal,
)
from app.news import (
    NewsService,
    RSSNewsSource,
    apply_trade_eligibility,
    build_news_classifier,
)
from app.news.service import normalize_item
from app.portfolio.paper_trading import PaperTradingService
from app.risk import RiskManager, RiskRules
from app.runtime import build_status
from app.signals import SignalCandidateService
from app.db import PersistenceRepository

settings = get_settings()
persistence = PersistenceRepository(settings.database_url, create_schema=False)
market_data_service = build_market_data_service(settings)
account_service = build_account_service(settings)
paper_trading_service = PaperTradingService(
    timeout=timedelta(minutes=settings.paper_position_timeout_minutes),
    starting_equity=settings.paper_starting_equity_usdt,
    repository=persistence,
    max_total_open_positions=settings.paper_max_total_open_positions,
    symbol_cooldown=timedelta(seconds=settings.paper_symbol_cooldown_seconds),
    global_entry_cooldown=timedelta(
        seconds=settings.paper_global_entry_cooldown_seconds
    ),
    max_daily_net_loss_pct=settings.paper_max_daily_net_loss_pct,
    max_weekly_net_loss_pct=settings.paper_max_weekly_net_loss_pct,
    max_account_drawdown_pct=settings.paper_max_account_drawdown_pct,
)
news_service = NewsService(
    [RSSNewsSource(url) for url in settings.news_rss_urls] if settings.news_enable_rss else [],
    build_news_classifier(settings),
    max_item_age=timedelta(minutes=settings.news_max_item_age_minutes),
    min_importance_to_classify=settings.news_min_importance_to_classify,
    min_classification_confidence=settings.signal_min_classification_confidence,
    codex_min_news_importance=settings.codex_cli_min_news_importance,
    classifier_version=settings.llm_classifier_version,
    classifier_cache_ttl=timedelta(seconds=settings.llm_cache_ttl_seconds),
    repository=persistence,
)
signal_candidate_service = SignalCandidateService(
    settings,
    news_service,
    market_data_service,
    account_service,
    paper_trading_service,
    persistence,
)
news_service.restore()
paper_trading_service.restore()
signal_candidate_service.restore()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    account_service.refresh_if_stale(force=True)
    await asyncio.to_thread(news_service.poll)
    market_data_service.refresh_all()
    signal_candidate_service.process_pending()
    signal_candidate_service.execute_ready_candidates()
    task = asyncio.create_task(news_polling_loop())
    signal_task = asyncio.create_task(signal_recheck_loop())
    try:
        yield
    finally:
        task.cancel()
        signal_task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        with suppress(asyncio.CancelledError):
            await signal_task


app = FastAPI(title="ByBot", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status")
def status() -> dict[str, object]:
    refresh_runtime_state(refresh_account=True)
    return build_status(
        settings,
        market_data_service,
        account_service,
        paper_trading_service,
        news_service,
        signal_candidate_service,
    )


@app.get("/news")
def news() -> dict[str, object]:
    return news_service.as_payload()


@app.get("/news/filtered")
def filtered_news() -> dict[str, object]:
    return {
        "items": [item.model_dump(mode="json") for item in news_service.filtered_items],
        "items_filtered_count": news_service.items_filtered_count,
    }


@app.get("/news/classifications")
def news_classifications() -> dict[str, object]:
    return {
        "classifications": [item.model_dump(mode="json") for item in news_service.classifications],
        "items_classified_count": news_service.items_classified_count,
        "mock_classifier_calls_count": news_service.mock_classifier_calls_count,
        "real_llm_calls_count": news_service.real_llm_calls_count,
        "llm_cache_hits": news_service.llm_cache_hits,
        "estimated_input_tokens": news_service.estimated_input_tokens,
        "estimated_output_tokens": news_service.estimated_output_tokens,
    }


@app.get("/news/classifier/status")
def news_classifier_status() -> dict[str, object]:
    return news_service.classifier_status_payload()


@app.get("/news/classifier/metrics")
def news_classifier_metrics() -> dict[str, object]:
    return news_service.classifier_metrics_payload()


@app.post("/news/classifier/test")
def test_news_classifier(request: ClassifierTestRequest) -> dict[str, object]:
    if not (settings.app_env.lower() == "local" and settings.test_mode):
        raise HTTPException(status_code=404, detail="classifier test endpoint is disabled")
    if news_service.classifier is None:
        raise HTTPException(status_code=503, detail="news classifier is disabled")
    item = normalize_item(
        NewsItem(
            title=request.title,
            summary=request.summary,
            source="local-classifier-test",
            published_at=datetime.now(timezone.utc),
        )
    )
    classification = apply_trade_eligibility(
        news_service.classifier.classify(item),
        minimum_confidence=settings.signal_min_classification_confidence,
    )
    return {
        "classification": classification.model_dump(mode="json"),
        "news_stored": False,
        "signal_created": False,
        "execution_attempted": False,
        "paper_position_opened": False,
        "exchange_order_placement": "blocked",
    }


@app.get("/news/filter-debug")
def news_filter_debug() -> dict[str, object]:
    return {"items": news_service.filter_debug_payload()}


@app.post("/news/test-item")
def news_test_item(item: NewsItem) -> dict[str, object]:
    accepted, reason, classification = news_service.ingest(item)
    return {
        "accepted": accepted,
        "reason": reason,
        "item": news_service.last_news_item.model_dump(mode="json") if news_service.last_news_item else None,
        "classification": classification.model_dump(mode="json") if classification else None,
        "filter_debug": (
            news_service.last_filter_debug.model_dump(mode="json")
            if news_service.last_filter_debug else None
        ),
    }


@app.get("/signals/candidates")
def signal_candidates() -> dict[str, object]:
    return {"candidates": signal_candidate_service.as_candidates_payload()}


@app.get("/signals/latest")
def latest_signal_candidate() -> dict[str, object]:
    result = signal_candidate_service.last_result
    return {"result": result.model_dump(mode="json") if result else None}


@app.get("/signals/dry-run")
def signal_dry_run() -> dict[str, object]:
    signal_candidate_service.process_pending()
    return {
        "results": signal_candidate_service.as_dry_run_payload(),
        "execution_attempted": False,
        "paper_position_opened": False,
    }


@app.get("/signals/pending")
def pending_signal_candidates() -> dict[str, object]:
    return {"candidates": signal_candidate_service.pending_payload()}


@app.get("/signals/history")
def signal_evaluation_history() -> dict[str, object]:
    return {"history": signal_candidate_service.history_payload()}


@app.post("/signals/test-from-news")
def test_signal_from_news(request: SignalTestFromNewsRequest) -> dict[str, object]:
    market_data_service.refresh_all()
    try:
        results = signal_candidate_service.process_news_id(
            request.news_id,
            allow_reprocess=request.reprocess,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "results": [result.model_dump(mode="json") for result in results],
        "execution_attempted": False,
        "paper_position_opened": False,
        "exchange_order_placement": "blocked",
    }


@app.get("/signals/{candidate_id}")
def signal_candidate(candidate_id: str) -> dict[str, object]:
    try:
        parsed_id = UUID(candidate_id)
        payload = signal_candidate_service.result_payload(parsed_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="signal candidate not found") from exc
    return payload


@app.post("/signals/{candidate_id}/recheck")
def recheck_signal_candidate(candidate_id: str) -> dict[str, object]:
    try:
        parsed_id = UUID(candidate_id)
        market_data_service.refresh_all()
        signal_candidate_service.recheck_candidate(parsed_id)
        payload = signal_candidate_service.result_payload(parsed_id)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="signal candidate not found") from exc
    return {
        "result": payload,
        "execution_attempted": False,
        "paper_position_opened": False,
    }


@app.post("/signals/{candidate_id}/test-market-snapshot")
def test_market_snapshot_for_signal(
    candidate_id: str,
    request: TestMarketSnapshotRequest,
) -> dict[str, object]:
    if not (
        settings.app_env.lower() == "local"
        and settings.test_mode
        and settings.bot_mode == BotMode.PAPER
    ):
        raise HTTPException(status_code=404, detail="test market snapshot endpoint is disabled")
    try:
        parsed_id = UUID(candidate_id)
        existing = signal_candidate_service.get_result(parsed_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="signal candidate not found") from exc
    symbol = existing.candidate.symbol
    if symbol is None:
        raise HTTPException(status_code=400, detail="candidate has no supported market symbol")

    now = datetime.now(timezone.utc)
    timestamp = request.timestamp or now
    if not request.fresh:
        timestamp = now - timedelta(
            seconds=settings.signal_confirmation_window_seconds + 1
        )
    snapshot = MarketSnapshot(
        symbol=symbol,
        timestamp=timestamp,
        last_price=request.price,
        bid_price=request.bid,
        ask_price=request.ask,
        price_change_1m_pct=request.price_change_1m_pct,
        simple_trend=request.trend_direction,
        simple_volatility=request.volatility_pct,
        volume_24h=request.volume_24h,
        trend_score=request.trend_score,
        volatility_pct=request.volatility_pct,
        liquidity_ok=request.ask >= request.bid,
        api_stable=True,
    )
    signal_candidate_service.recheck_candidate_with_snapshot(
        parsed_id,
        snapshot,
        volume_change_pct=request.volume_change_pct,
        volume_spike=request.volume_spike,
        now=now,
    )
    payload = signal_candidate_service.result_payload(parsed_id)
    return {
        "candidate": payload["candidate"],
        "risk_preview": payload["risk_preview"],
        "test_snapshot_used": request.model_dump(mode="json"),
        "execution_attempted": False,
        "paper_position_opened": False,
        "exchange_order_placement": "blocked",
    }


@app.get("/market")
def market() -> dict[str, object]:
    market_data_service.refresh_all()
    return market_data_service.as_payload()


@app.get("/market/{symbol}")
def market_symbol(symbol: str) -> dict[str, object]:
    try:
        parsed_symbol = Symbol(symbol.upper())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unsupported symbol") from exc

    market_data_service.refresh_all()
    snapshot = market_data_service.latest_snapshot(parsed_symbol)
    if snapshot is None:
        return {
            "status": market_data_service.status,
            "last_error": market_data_service.last_error,
            "snapshot": None,
        }

    return {
        "status": market_data_service.status,
        "last_error": market_data_service.last_error,
        "snapshot": snapshot_to_payload(snapshot),
    }


@app.get("/account")
def account() -> dict[str, object]:
    account_service.refresh_if_stale(force=True)
    return account_service.as_payload()


@app.post("/paper/test-signal")
def paper_test_signal(request: PaperTestSignalRequest) -> dict[str, object]:
    refresh_runtime_state(refresh_account=True)
    if market_data_service.status != "OK":
        return {
            "accepted": False,
            "reason": "market data unavailable",
            "market_status": market_data_service.status,
            "last_error": market_data_service.last_error,
        }

    market_snapshot = market_data_service.latest_snapshot(request.symbol)
    if market_snapshot is None:
        return {
            "accepted": False,
            "reason": "market snapshot unavailable",
            "market_status": market_data_service.status,
        }

    signal = TradeSignal(
        action=SignalAction.TRADE,
        symbol=request.symbol,
        side=request.side,
        confidence=request.confidence,
        expected_edge_bps=request.expected_edge_bps,
        stop_loss_pct=request.stop_loss_pct,
        take_profit_pct=request.take_profit_pct or settings.paper_take_profit_pct,
        reasons=["manual paper test signal"],
    )
    equity = paper_trading_service.equity
    risk_context = RiskContext(
        equity=equity,
        available_balance=paper_trading_service.equity,
        requested_risk_pct=request.requested_risk_pct or settings.max_risk_per_trade_pct,
        leverage=request.leverage or settings.max_leverage,
        open_positions=len(paper_trading_service.open_positions),
        daily_pnl_pct=settings.paper_daily_pnl_pct,
        weekly_pnl_pct=settings.paper_weekly_pnl_pct,
        consecutive_losses=settings.paper_consecutive_losses,
        api_stable=market_data_service.status == "OK",
    )
    risk_rules = RiskRules(
        max_open_positions=settings.paper_max_total_open_positions,
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
    risk_decision = RiskManager(risk_rules).assess(signal, market_snapshot, risk_context)
    if not risk_decision.approved:
        paper_trading_service.last_risk_decision = risk_decision
        return {
            "accepted": False,
            "reason": "risk manager rejected signal",
            "risk_decision": risk_decision.model_dump(mode="json"),
        }

    position = paper_trading_service.open_from_signal(
        signal,
        risk_decision,
        market_snapshot,
        take_profit_pct=request.take_profit_pct or settings.paper_take_profit_pct,
    )
    return {
        "accepted": True,
        "position": position.model_dump(mode="json"),
        "risk_decision": risk_decision.model_dump(mode="json"),
        "real_exchange_execution": "blocked",
    }


@app.get("/paper/positions")
def paper_positions() -> dict[str, object]:
    refresh_runtime_state(refresh_account=False)
    return {"positions": paper_trading_service.positions_payload()}


@app.post("/paper/close-position")
def paper_close_position() -> dict[str, object]:
    refresh_runtime_state(refresh_account=False)
    open_position = paper_trading_service.open_position
    if open_position is None:
        return {
            "closed": False,
            "reason": "no open paper position",
            "last_trade": (
                paper_trading_service.closed_trades[-1].model_dump(mode="json")
                if paper_trading_service.closed_trades
                else None
            ),
        }

    market_snapshot = market_data_service.latest_snapshot(open_position.symbol)
    if market_data_service.status != "OK" or market_snapshot is None:
        return {
            "closed": False,
            "reason": "market data unavailable",
            "market_status": market_data_service.status,
            "last_error": market_data_service.last_error,
        }

    closed = paper_trading_service.close_position(
        market_snapshot.last_price,
        reason="manual_close",
    )
    return {
        "closed": True,
        "position": closed.model_dump(mode="json"),
        "pnl": paper_trading_service.pnl().model_dump(mode="json"),
        "real_exchange_execution": "blocked",
    }


@app.get("/paper/trades")
def paper_trades() -> dict[str, object]:
    refresh_runtime_state(refresh_account=False)
    return {"trades": paper_trading_service.trades_payload()}


@app.get("/paper/pnl")
def paper_pnl() -> dict[str, object]:
    refresh_runtime_state(refresh_account=False)
    return paper_trading_service.pnl().model_dump(mode="json")


def refresh_runtime_state(*, refresh_account: bool) -> None:
    market_data_service.refresh_all()
    if refresh_account:
        account_service.refresh_if_stale()
    monitor_paper_positions()


def monitor_paper_positions() -> None:
    for snapshot in market_data_service.latest_snapshots():
        paper_trading_service.update_from_market(snapshot)
    signal_candidate_service.sync_paper_states()


def _require_local_test_mode() -> None:
    if not (settings.app_env.lower() == "local" and settings.test_mode):
        raise HTTPException(status_code=404, detail="paper test endpoint is disabled")


@app.post("/paper/test/execute-candidate/{candidate_id}")
def paper_test_execute_candidate(candidate_id: str) -> dict[str, object]:
    _require_local_test_mode()
    try:
        result = signal_candidate_service.execute_ready_candidate(
            UUID(candidate_id), force=True
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="signal candidate not found") from exc
    execution_record = (
        persistence.paper_execution_details(candidate_id)
        if persistence else None
    )
    return {
        "result": result.model_dump(mode="json"),
        "position": paper_trading_service.last_execution_details,
        "execution_attempted": result.execution_attempted,
        "paper_position_opened": result.paper_position_opened,
        "duplicate": paper_trading_service.last_execution_duplicate,
        "block_reason": result.execution_block_reason,
        "error_code": result.execution_error_code,
        "retryable": result.execution_retryable,
        "risk_decision_id": result.risk_preview.risk_decision_id,
        "execution_record": execution_record,
        "paper_positions": paper_trading_service.positions_payload(),
        "exchange_order_placement": "blocked",
    }


@app.post("/paper/test/market-snapshot")
def paper_test_market_snapshot(
    request: PaperMarketSnapshotTestRequest,
) -> dict[str, object]:
    _require_local_test_mode()
    snapshot = MarketSnapshot(
        symbol=request.symbol,
        timestamp=request.timestamp or datetime.now(timezone.utc),
        last_price=request.price,
        bid_price=request.bid,
        ask_price=request.ask,
        trend_score=0,
        volatility_pct=0,
        liquidity_ok=True,
    )
    position = paper_trading_service.update_from_market(snapshot)
    signal_candidate_service.sync_paper_states()
    return {
        "position": position.model_dump(mode="json") if position else None,
        "pnl": paper_trading_service.pnl().model_dump(mode="json"),
        "exchange_order_placement": "blocked",
    }


@app.post("/paper/test/close/{position_id}")
def paper_test_close(position_id: str) -> dict[str, object]:
    _require_local_test_mode()
    position = next(
        (
            item for item in paper_trading_service.open_positions
            if str(item.id) == position_id
        ),
        None,
    )
    if position is None:
        raise HTTPException(status_code=404, detail="open paper position not found")
    closed = paper_trading_service.close_position(
        position.current_price, reason="manual_close", position_id=position.id
    )
    signal_candidate_service.sync_paper_states()
    return {
        "position": closed.model_dump(mode="json"),
        "pnl": paper_trading_service.pnl().model_dump(mode="json"),
        "exchange_order_placement": "blocked",
    }


@app.post("/paper/test/reset-kill-switch")
def paper_test_reset_kill_switch() -> dict[str, object]:
    _require_local_test_mode()
    return {
        "risk_control": paper_trading_service.reset_kill_switch(),
        "exchange_order_placement": "blocked",
    }


async def news_polling_loop() -> None:
    while True:
        await asyncio.sleep(settings.news_poll_interval_seconds)
        await asyncio.to_thread(news_service.poll)
        market_data_service.refresh_all()
        signal_candidate_service.process_pending()
        signal_candidate_service.execute_ready_candidates()
        monitor_paper_positions()


async def signal_recheck_loop() -> None:
    while True:
        await asyncio.sleep(settings.signal_reevaluation_interval_seconds)
        await asyncio.to_thread(market_data_service.refresh_all)
        signal_candidate_service.reevaluate_pending()
        signal_candidate_service.execute_ready_candidates()
        monitor_paper_positions()
