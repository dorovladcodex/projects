from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.main as main_module

from app.bybit.market_data import MarketDataService
from app.bybit.private import build_account_service
from app.config import Settings
from app.models import (
    MarketSnapshot,
    NewsClassification,
    NewsItem,
    NewsSignalAction,
    Sentiment,
    SignalTestFromNewsRequest,
    Symbol,
)
from app.news import MockNewsClassifier, NewsService
from app.portfolio.paper_trading import PaperTradingService
from app.runtime import build_status
from app.signals import SignalCandidateService


class SequenceProvider:
    def __init__(self, prices: tuple[float, float], *, stale: bool = False) -> None:
        self.prices = prices
        self.calls = 0
        self.stale = stale

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

    assert result.candidate.action == NewsSignalAction.BUY
    assert result.candidate.symbol == Symbol.BTCUSDT
    assert result.candidate.market_confirmation.direction_confirmed is True
    assert result.risk_preview is not None and result.risk_preview.approved is True
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

    assert result.candidate.action == NewsSignalAction.SELL
    assert result.candidate.sentiment == Sentiment.BEARISH
    assert result.risk_preview is not None and result.risk_preview.approved is True


def test_neutral_and_low_confidence_classifications_create_no_trade() -> None:
    signals, _, classification = pipeline(
        "SEC reviews Bitcoin ETF framework",
        "The BTC regulatory review continues.",
    )
    classification.sentiment = Sentiment.NEUTRAL
    classification.confidence = 0.9
    neutral = signals.process_news_id(classification.news_id)[0]
    assert neutral.candidate.action == NewsSignalAction.NO_TRADE
    assert "neutral classification" in neutral.candidate.reasons

    low_signals, _, low_classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
    )
    low_classification.confidence = 0.5
    low = low_signals.process_news_id(low_classification.news_id)[0]
    assert low.candidate.action == NewsSignalAction.NO_TRADE
    assert "classification confidence below signal threshold" in low.candidate.reasons


def test_conflicting_or_stale_market_data_blocks_candidate() -> None:
    conflict_signals, _, conflict_classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
        prices=(100.0, 99.75),
    )
    conflict = conflict_signals.process_news_id(conflict_classification.news_id)[0]
    assert conflict.candidate.action == NewsSignalAction.NO_TRADE
    assert "market direction conflicts with news" in conflict.candidate.reasons

    stale_signals, _, stale_classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
        stale=True,
    )
    stale_result = stale_signals.process_news_id(stale_classification.news_id)[0]
    assert stale_result.candidate.action == NewsSignalAction.NO_TRADE
    assert "market data is stale" in stale_result.candidate.reasons


def test_insufficient_edge_and_duplicate_classification_are_blocked_safely() -> None:
    signals, paper, classification = pipeline(
        "SEC approves Bitcoin ETF",
        "BTC ETF approval.",
        prices=(100.0, 100.06),
    )

    first = signals.process_news_id(classification.news_id)
    second = signals.process_news_id(classification.news_id)

    assert first[0].candidate.action == NewsSignalAction.NO_TRADE
    assert "expected edge after costs is insufficient" in first[0].candidate.reasons
    assert second[0].candidate.id == first[0].candidate.id
    assert len(signals.candidates) == 1
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
        status = build_status(
            signals.settings,
            signals.market_data,
            signals.account_service,
            paper,
            signals.news_service,
            signals,
        )

        assert posted["results"][0]["candidate"]["action"] == "BUY"
        assert posted["execution_attempted"] is False
        assert posted["paper_position_opened"] is False
        assert len(candidates["candidates"]) == 1
        assert latest["result"]["risk_preview"]["approved"] is True
        assert dry_run["execution_attempted"] is False
        assert status["last_signal_candidate"]["action"] == "BUY"
        assert status["signal_candidates_count"] == 1
        assert status["no_trade_candidates_count"] == 0
        assert status["risk_preview_approved_count"] == 1
        assert status["risk_preview_blocked_count"] == 0
        assert paper.open_position is None
    finally:
        main_module.signal_candidate_service = previous_signal_service
        main_module.market_data_service = previous_market
        main_module.news_service = previous_news
