from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import (
    MarketSnapshot,
    NewsClassification,
    NewsItem,
    Sentiment,
    Side,
    SignalAction,
    Symbol,
    TradeSignal,
)


@dataclass(frozen=True)
class StrategyRules:
    max_news_age: timedelta = timedelta(minutes=30)
    min_importance: float = 0.7
    min_confidence: float = 0.7
    min_abs_trend_score: float = 0.2
    max_spread_bps: float = 8.0
    min_volatility_pct: float = 0.1
    max_volatility_pct: float = 8.0
    min_expected_edge_bps: float = 12.0
    estimated_cost_bps: float = 8.0
    stop_loss_pct: float = 0.5


class NewsMomentumStrategy:
    def __init__(self, rules: StrategyRules | None = None) -> None:
        self.rules = rules or StrategyRules()

    def evaluate(
        self,
        news: NewsItem,
        classification: NewsClassification,
        market: MarketSnapshot,
        *,
        now: datetime | None = None,
    ) -> TradeSignal:
        now = now or datetime.now(timezone.utc)
        reasons: list[str] = []

        expected_symbol = Symbol(f"{news.asset_hint.value}USDT")
        if classification.news_id != news.id:
            reasons.append("classification does not match news item")
        if market.symbol != expected_symbol:
            reasons.append("market symbol does not match news asset")
        if now - news.published_at > self.rules.max_news_age:
            reasons.append("news is stale")
        if news.published_at > now + timedelta(minutes=1):
            reasons.append("news timestamp is in the future")
        if news.importance < self.rules.min_importance:
            reasons.append("news importance is too low")
        if classification.sentiment == Sentiment.NEUTRAL:
            reasons.append("news sentiment is neutral")
        if classification.confidence < self.rules.min_confidence:
            reasons.append("classification confidence is too low")
        if not market.api_stable:
            reasons.append("market data API is unstable")
        if not market.liquidity_ok:
            reasons.append("liquidity is insufficient")
        if market.spread_bps > self.rules.max_spread_bps:
            reasons.append("spread is too wide")
        if not self.rules.min_volatility_pct <= market.volatility_pct <= self.rules.max_volatility_pct:
            reasons.append("volatility is outside allowed range")

        direction = 1 if classification.sentiment == Sentiment.BULLISH else -1
        if direction * market.trend_score < self.rules.min_abs_trend_score:
            reasons.append("market trend does not confirm news direction")

        gross_edge_bps = abs(market.trend_score) * 50 * classification.confidence
        expected_edge_bps = gross_edge_bps - self.rules.estimated_cost_bps
        if expected_edge_bps < self.rules.min_expected_edge_bps:
            reasons.append("expected edge after costs is too small")

        if reasons:
            return TradeSignal(
                action=SignalAction.NO_TRADE,
                symbol=market.symbol,
                confidence=classification.confidence,
                expected_edge_bps=expected_edge_bps,
                reasons=reasons,
            )

        return TradeSignal(
            action=SignalAction.TRADE,
            symbol=market.symbol,
            side=Side.BUY if direction > 0 else Side.SELL,
            confidence=classification.confidence,
            expected_edge_bps=expected_edge_bps,
            stop_loss_pct=self.rules.stop_loss_pct,
            reasons=["fresh important news confirmed by market and expected edge"],
        )
