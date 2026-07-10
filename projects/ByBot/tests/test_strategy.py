from datetime import datetime, timedelta, timezone

from app.models import (
    Asset,
    MarketSnapshot,
    NewsClassification,
    NewsItem,
    Sentiment,
    SignalAction,
    Side,
    Symbol,
)
from app.strategy import NewsMomentumStrategy


def inputs() -> tuple[NewsItem, NewsClassification, MarketSnapshot]:
    news = NewsItem(
        title="Bitcoin ETF records major inflow",
        summary="Fresh institutional demand was reported.",
        source="test",
        published_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        asset_hint=Asset.BTC,
        importance=0.9,
    )
    classification = NewsClassification(
        news_id=news.id,
        sentiment=Sentiment.BULLISH,
        confidence=0.9,
        rationale="positive",
        model_name="test",
    )
    market = MarketSnapshot(
        symbol=Symbol.BTCUSDT,
        timestamp=datetime.now(timezone.utc),
        last_price=60_000,
        bid_price=59_999,
        ask_price=60_001,
        trend_score=0.7,
        volatility_pct=2.0,
        liquidity_ok=True,
    )
    return news, classification, market


def test_strategy_trades_when_all_gates_pass() -> None:
    news, classification, market = inputs()

    signal = NewsMomentumStrategy().evaluate(news, classification, market)

    assert signal.action == SignalAction.TRADE
    assert signal.side == Side.BUY
    assert signal.stop_loss_pct is not None


def test_strategy_no_trade_for_stale_news() -> None:
    news, classification, market = inputs()
    news.published_at = datetime.now(timezone.utc) - timedelta(hours=2)

    signal = NewsMomentumStrategy().evaluate(news, classification, market)

    assert signal.action == SignalAction.NO_TRADE
    assert "news is stale" in signal.reasons


def test_strategy_no_trade_when_trend_disagrees() -> None:
    news, classification, market = inputs()
    market.trend_score = -0.6

    signal = NewsMomentumStrategy().evaluate(news, classification, market)

    assert signal.action == SignalAction.NO_TRADE
    assert "market trend does not confirm news direction" in signal.reasons
