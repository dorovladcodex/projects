from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import app.main as main_module
import pytest
from fastapi import HTTPException

from app.bybit.market_data import MarketDataService
from app.bybit.private import build_account_service
from app.config import Settings
from app.models import (
    CandidateLifecycleState,
    MarketSnapshot,
    NewsClassification,
    NewsItem,
    NewsSignalAction,
    Sentiment,
    SignalTestFromNewsRequest,
    TestMarketSnapshotRequest as MarketSnapshotTestRequest,
    Symbol,
)
from app.news import MockNewsClassifier, NewsService
from app.portfolio.paper_trading import PaperTradingService
from app.runtime import build_status
from app.signals import SignalCandidateService


class SequenceProvider:
    def __init__(self, prices: tuple[float, float], *, stale: bool = False) -> None:
        self.prices = list(prices)
        self.calls = 0
        self.stale = stale

    def add_price(self, price: float) -> None:
        self.prices.append(price)

    def get_snapshot(self, symbol: Symbol) -> MarketSnapshot:
        price = self.prices[min(self.calls, len(self.prices) - 1)]
        offset = 130 if self.stale else max(0, 5 - self.calls)
        self.calls += 1
        return MarketSnapshot(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=offset),
            last_price=price,
            bid_price=price - 0.01,
            ask_price=price + 0.01,
            trend_score=0,
            volatility_pct=0,
            liquidity_ok=True,
            volume_24h=10_000 + self.calls * 100,
        )


def pipeline(
    title: str,
    summary: str,
    *,
    prices: tuple[float, float] = (100.0, 100.25),
    stale: bool = False,
    settings: Settings | None = None,
) -> tuple[SignalCandidateService, PaperTradingService, NewsClassification]:
    settings = settings or Settings(
        bybit_api_key=None,
        bybit_api_secret=None,
        news_enable_rss=False,
    )
    news = NewsService([], MockNewsClassifier(), max_item_age=timedelta(minutes=60))
    accepted, _, classification = news.ingest(
        NewsItem(
            title=title,
            summary=summary,
            source="test",
            published_at=datetime.now(timezone.utc),
        )
    )
    assert accepted and classification is not None
    market = MarketDataService(SequenceProvider(prices, stale=stale), [Symbol.BTCUSDT])
    market.refresh_all()
    market.refresh_all()
    paper = PaperTradingService()
    signals = SignalCandidateService(
        settings,
        news,
        market,
        build_account_service(settings),
        paper,
    )
    return signals, paper, classification


def test_bullish_btc_etf_news_creates_buy_candidate_and_risk_preview() -> None:
    signals, paper, classification = pipeline(
        "SEC approves spot Bitcoin ETF from BlackRock",
        "ETF approval is confirmed.",
    )

    result = signals.process_news_id(classification.news_id)[0]

    assert result.candidate.state == CandidateLifecycleState.READY
    assert result.candidate.proposed_action == NewsSignalAction.BUY
    assert result.candidate.final_action == NewsSignalAction.BUY
    assert result.candidate.symbol == Symbol.BTCUSDT
    assert result.candidate.market_confirmation.direction_confirmed is True
    assert result.risk_preview.preview_performed is True
    assert result.risk_preview.approved is True
    assert result.risk_preview.capped_size > 0
    assert result.risk_preview.position_notional > 0
    assert paper.open_position is None
    assert result.execution_attempted is False


def test_bearish_btc_hack_news_creates_sell_candidate() -> None:
    signals, _, classification = pipeline(
        "Bitcoin exchange hack triggers liquidation",
        "A BTC exploit caused a liquidation event.",
        prices=(100.0, 99.75),
    )

    result = signals.process_news_id(classification.news_id)[0]

    assert result.candidate.state == CandidateLifecycleState.READY
    assert result.candidate.final_action == NewsSignalAction.SELL
    assert result.candidate.sentiment == Sentiment.BEARISH
    assert result.risk_preview.preview_performed is True
    assert result.risk_preview.approved is True


def test_neutral_and_low_confidence_classifications_create_no_trade() -> None:
    signals, _, classification = pipeline(
        "SEC reviews Bitcoin ETF framework",
        "The BTC regulatory review continues.",
    )
    classification.sentiment = Sentiment.NEUTRAL
    classification.confidence = 0.9
    neutral = signals.process_news_id(classification.news_id)
    assert neutral == []
    assert signals.candidates == []

    low_signals, _, low_classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
    )
    low_classification.confidence = 0.5
    low = low_signals.process_news_id(low_classification.news_id)
    assert low == []
    assert low_signals.candidates == []


def test_strong_conflict_blocks_but_stale_market_waits() -> None:
    conflict_signals, _, conflict_classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
        prices=(100.0, 99.6),
    )
    conflict = conflict_signals.process_news_id(conflict_classification.news_id)[0]
    assert conflict.candidate.state == CandidateLifecycleState.BLOCKED
    assert conflict.candidate.final_action == NewsSignalAction.NO_TRADE
    assert "market moved against news beyond conflict threshold" in conflict.candidate.reasons
    assert conflict.risk_preview.preview_performed is False

    stale_signals, _, stale_classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
        stale=True,
    )
    stale_result = stale_signals.process_news_id(stale_classification.news_id)[0]
    assert stale_result.candidate.state == CandidateLifecycleState.PENDING_CONFIRMATION
    assert stale_result.candidate.final_action == NewsSignalAction.NO_TRADE
    assert "market data is stale" in stale_result.candidate.reasons


def test_insufficient_edge_and_duplicate_classification_are_blocked_safely() -> None:
    signals, paper, classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
        prices=(100.0, 100.06),
    )

    first = signals.process_news_id(classification.news_id)
    second = signals.process_news_id(classification.news_id)

    assert first[0].candidate.state == CandidateLifecycleState.PENDING_CONFIRMATION
    assert first[0].candidate.final_action == NewsSignalAction.NO_TRADE
    assert "expected edge after costs is insufficient" in first[0].candidate.reasons
    assert second[0].candidate.id == first[0].candidate.id
    assert len(signals.candidates) == 1
    assert first[0].risk_preview.preview_performed is False
    assert paper.open_position is None


def test_sideways_candidate_becomes_ready_after_later_confirmation() -> None:
    signals, paper, classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
        prices=(100.0, 100.0),
    )
    initial = signals.process_news_id(classification.news_id)[0]
    candidate_id = initial.candidate.id

    assert initial.candidate.state == CandidateLifecycleState.PENDING_CONFIRMATION
    assert initial.candidate.proposed_action == NewsSignalAction.BUY
    assert initial.candidate.final_action == NewsSignalAction.NO_TRADE
    assert initial.risk_preview.preview_performed is False
    assert len(initial.candidate.evaluation_history) == 1
    assert signals.no_trade_candidates_count == 0

    provider = signals.market_data.provider
    assert isinstance(provider, SequenceProvider)
    provider.add_price(100.25)
    signals.market_data.refresh_all()
    updated = signals.recheck_candidate(candidate_id)

    assert updated.candidate.id == candidate_id
    assert updated.candidate.state == CandidateLifecycleState.READY
    assert updated.candidate.final_action == NewsSignalAction.BUY
    assert updated.risk_preview.preview_performed is True
    assert len(updated.candidate.evaluation_history) == 2
    assert len(signals.candidates) == 1
    assert paper.open_position is None


def test_pending_candidate_expires_without_risk_preview() -> None:
    settings = Settings(
        bybit_api_key=None,
        bybit_api_secret=None,
        news_enable_rss=False,
        signal_ttl_seconds=1,
    )
    signals, paper, classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
        prices=(100.0, 100.0),
        settings=settings,
    )
    result = signals.process_news_id(classification.news_id)[0]

    expired = signals.recheck_candidate(
        result.candidate.id,
        now=result.candidate.expires_at + timedelta(milliseconds=1),
    )

    assert expired.candidate.state == CandidateLifecycleState.EXPIRED
    assert expired.candidate.final_action == NewsSignalAction.NO_TRADE
    assert expired.candidate.reasons == [
        "market confirmation was not received before signal expiry"
    ]
    assert expired.risk_preview.preview_performed is False
    assert "trade side is missing" not in expired.risk_preview.rejection_reasons
    assert signals.no_trade_candidates_count == 1
    assert signals.state_count(CandidateLifecycleState.EXPIRED) == 1
    assert paper.open_position is None


def test_concurrent_background_rechecks_add_only_one_evaluation() -> None:
    settings = Settings(
        bybit_api_key=None,
        bybit_api_secret=None,
        news_enable_rss=False,
        signal_reevaluation_interval_seconds=5,
    )
    signals, paper, classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
        prices=(100.0, 100.0),
        settings=settings,
    )
    result = signals.process_news_id(classification.news_id)[0]
    first_time = result.candidate.evaluation_history[0].evaluated_at
    recheck_time = first_time + timedelta(seconds=6)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(signals.reevaluate_pending, now=recheck_time)
            for _ in range(2)
        ]
        [future.result() for future in futures]

    assert len(result.candidate.evaluation_history) == 2
    assert result.candidate.state == CandidateLifecycleState.PENDING_CONFIRMATION
    assert len(signals.candidates) == 1
    assert signals.no_trade_candidates_count == 0
    assert paper.open_position is None


def test_signal_endpoints_return_dry_run_without_opening_position() -> None:
    signals, paper, classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
    )
    previous_signal_service = main_module.signal_candidate_service
    previous_market = main_module.market_data_service
    previous_news = main_module.news_service
    try:
        main_module.signal_candidate_service = signals
        main_module.market_data_service = signals.market_data
        main_module.news_service = signals.news_service

        posted = main_module.test_signal_from_news(
            SignalTestFromNewsRequest(news_id=classification.news_id)
        )
        candidates = main_module.signal_candidates()
        latest = main_module.latest_signal_candidate()
        dry_run = main_module.signal_dry_run()
        candidate_id = posted["results"][0]["candidate"]["id"]
        one_candidate = main_module.signal_candidate(candidate_id)
        pending = main_module.pending_signal_candidates()
        history = main_module.signal_evaluation_history()
        rechecked = main_module.recheck_signal_candidate(candidate_id)
        status = build_status(
            signals.settings,
            signals.market_data,
            signals.account_service,
            paper,
            signals.news_service,
            signals,
        )

        assert posted["results"][0]["candidate"]["state"] == "READY"
        assert posted["results"][0]["candidate"]["final_action"] == "BUY"
        assert posted["execution_attempted"] is False
        assert posted["paper_position_opened"] is False
        assert len(candidates["candidates"]) == 1
        assert latest["result"]["risk_preview"]["preview_performed"] is True
        assert latest["result"]["risk_preview"]["approved"] is True
        assert dry_run["execution_attempted"] is False
        assert one_candidate["candidate"]["id"] == candidate_id
        assert pending["candidates"] == []
        assert len(history["history"][0]["evaluations"]) == 1
        assert rechecked["execution_attempted"] is False
        assert rechecked["paper_position_opened"] is False
        assert status["last_signal_candidate"]["state"] == "READY"
        assert status["last_signal_candidate"]["final_action"] == "BUY"
        assert status["signal_candidates_count"] == 1
        assert status["no_trade_candidates_count"] == 0
        assert status["risk_preview_approved_count"] == 1
        assert status["risk_preview_blocked_count"] == 0
        assert status["pending_signal_candidates_count"] == 0
        assert status["ready_signal_candidates_count"] == 1
        assert status["blocked_signal_candidates_count"] == 0
        assert status["expired_signal_candidates_count"] == 0
        assert status["last_signal_evaluation_at"] is not None
        assert paper.open_position is None
    finally:
        main_module.signal_candidate_service = previous_signal_service
        main_module.market_data_service = previous_market
        main_module.news_service = previous_news


def snapshot_request(
    *,
    change: float,
    trend: str,
    trend_score: float,
    bid: float = 100.29,
    ask: float = 100.31,
    volatility: float = 1.0,
    fresh: bool = True,
) -> MarketSnapshotTestRequest:
    return MarketSnapshotTestRequest(
        price=100.30,
        bid=bid,
        ask=ask,
        price_change_1m_pct=change,
        trend_direction=trend,
        trend_score=trend_score,
        volatility_pct=volatility,
        volume_24h=25_000,
        volume_change_pct=25,
        volume_spike=True,
        fresh=fresh,
    )


def local_test_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    title: str,
    summary: str,
) -> tuple[SignalCandidateService, PaperTradingService, NewsClassification]:
    settings = Settings(
        app_env="local",
        test_mode=True,
        bot_mode="PAPER",
        auto_paper_execution=False,
        bybit_enable_trading=False,
        bybit_api_key=None,
        bybit_api_secret=None,
        news_enable_rss=False,
    )
    signals, paper, classification = pipeline(
        title,
        summary,
        prices=(100.0, 100.0),
        settings=settings,
    )
    signals.process_news_id(classification.news_id)
    monkeypatch.setattr(main_module, "settings", settings)
    monkeypatch.setattr(main_module, "signal_candidate_service", signals)
    return signals, paper, classification


def test_local_bullish_snapshot_makes_pending_candidate_ready_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals, paper, _ = local_test_pipeline(
        monkeypatch,
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
    )
    candidate = signals.candidates[0]
    history_before = len(signals.market_data.history[Symbol.BTCUSDT])
    global_snapshot_before = signals.market_data.latest_snapshot(Symbol.BTCUSDT)

    response = main_module.test_market_snapshot_for_signal(
        str(candidate.id),
        snapshot_request(change=0.30, trend="BULLISH", trend_score=0.6),
    )

    assert response["candidate"]["state"] == "READY"
    assert response["candidate"]["final_action"] == "BUY"
    assert response["candidate"]["evaluation_history"][-1]["market_confirmed"] is True
    assert response["candidate"]["expected_edge_bps"] > 0
    assert response["risk_preview"]["preview_performed"] is True
    assert response["execution_attempted"] is False
    assert response["paper_position_opened"] is False
    assert response["exchange_order_placement"] == "blocked"
    assert len(signals.market_data.history[Symbol.BTCUSDT]) == history_before
    assert signals.market_data.latest_snapshot(Symbol.BTCUSDT) == global_snapshot_before
    assert paper.open_position is None


def test_local_bearish_snapshot_makes_pending_candidate_ready_sell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals, paper, _ = local_test_pipeline(
        monkeypatch,
        "Bitcoin exchange hack triggers liquidation",
        "A BTC exploit caused liquidation.",
    )
    response = main_module.test_market_snapshot_for_signal(
        str(signals.candidates[0].id),
        snapshot_request(change=-0.30, trend="BEARISH", trend_score=-0.6),
    )

    assert response["candidate"]["state"] == "READY"
    assert response["candidate"]["final_action"] == "SELL"
    assert response["risk_preview"]["preview_performed"] is True
    assert paper.open_position is None


@pytest.mark.parametrize(
    ("market_request", "expected_state"),
    [
        (snapshot_request(change=0.0, trend="SIDEWAYS", trend_score=0.0), "PENDING_CONFIRMATION"),
        (snapshot_request(change=-0.2, trend="BEARISH", trend_score=-0.4), "PENDING_CONFIRMATION"),
        (snapshot_request(change=0.3, trend="BULLISH", trend_score=0.6, fresh=False), "PENDING_CONFIRMATION"),
        (snapshot_request(change=0.3, trend="BULLISH", trend_score=0.6, bid=99, ask=101), "PENDING_CONFIRMATION"),
        (snapshot_request(change=0.3, trend="BULLISH", trend_score=0.6, volatility=9), "PENDING_CONFIRMATION"),
    ],
)
def test_non_tradeable_test_snapshots_do_not_make_candidate_ready(
    monkeypatch: pytest.MonkeyPatch,
    market_request: MarketSnapshotTestRequest,
    expected_state: str,
) -> None:
    signals, paper, _ = local_test_pipeline(
        monkeypatch,
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
    )

    response = main_module.test_market_snapshot_for_signal(
        str(signals.candidates[0].id), market_request
    )

    assert response["candidate"]["state"] == expected_state
    assert response["candidate"]["final_action"] == "NO_TRADE"
    assert response["risk_preview"]["preview_performed"] is False
    assert paper.open_position is None


def test_market_snapshot_endpoint_is_hidden_outside_local_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals, _, _ = local_test_pipeline(
        monkeypatch,
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
    )
    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(app_env="production", test_mode=False, news_enable_rss=False),
    )

    with pytest.raises(HTTPException) as exc:
        main_module.test_market_snapshot_for_signal(
            str(signals.candidates[0].id),
            snapshot_request(change=0.3, trend="BULLISH", trend_score=0.6),
        )

    assert exc.value.status_code == 404
