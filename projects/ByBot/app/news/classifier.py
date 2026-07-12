from __future__ import annotations

from typing import Protocol

from app.models import Asset, NewsClassification, NewsItem, Sentiment
from app.news.keywords import KeywordMatcher
from app.news.text import clean_news_text


class LLMNewsClassifier(Protocol):
    def classify(self, item: NewsItem) -> NewsClassification: ...


# Backwards-compatible name used by early strategy code.
NewsClassifier = LLMNewsClassifier


class MockNewsClassifier:
    """Deterministic classifier used for tests and local development."""

    def classify(self, item: NewsItem) -> NewsClassification:
        text = clean_news_text(f"{item.title} {item.summary}")
        matcher = KeywordMatcher()
        bullish_terms = (
            "etf approval", "sec approves", "approval", "blackrock launches",
            "blackrock receives approval", "institutional adoption",
            "exchange restores withdrawals", "rate cut", "major legal approval",
        )
        bearish_terms = (
            "etf rejected", "etf delayed", "hack", "exploit", "exchange outage",
            "withdrawals suspended", "lawsuit", "charges", "ban", "delisting",
            "rate hike", "liquidation event", "liquidation",
        )
        bullish_matches = matcher.find_matches(text, bullish_terms)
        bearish_matches = matcher.find_matches(text, bearish_terms)
        if bullish_matches and bearish_matches:
            sentiment, confidence = Sentiment.NEUTRAL, 0.55
            reason = "Mock keyword classification found conflicting bullish and bearish terms."
        elif bullish_matches:
            sentiment, confidence = Sentiment.BULLISH, 0.9
            reason = f"Mock keyword classification found bullish terms: {', '.join(bullish_matches)}."
        elif bearish_matches:
            sentiment, confidence = Sentiment.BEARISH, 0.9
            reason = f"Mock keyword classification found bearish terms: {', '.join(bearish_matches)}."
        else:
            sentiment, confidence = Sentiment.NEUTRAL, 0.5
            reason = "Mock keyword classification found no directional terms."

        category = _category(text, matcher)
        urgency = "high" if _is_high_urgency(text, matcher) else "normal"
        asset = _asset_from_text(text, matcher)
        return NewsClassification(
            news_id=item.id,
            asset=asset,
            sentiment=sentiment,
            confidence=confidence,
            category=category,
            urgency=urgency,
            reason=reason,
            rationale=reason,
            model_name="mock-keyword-v1",
        )


def _category(text: str, matcher: KeywordMatcher) -> str:
    if matcher.contains(text, "etf"):
        return "etf"
    if matcher.find_matches(text, ("hack", "exploit")):
        return "security"
    if matcher.find_matches(text, ("sec", "mica", "clarity act", "regulation")):
        return "regulation"
    if matcher.find_matches(text, ("fed", "cpi", "rate cut", "rate hike")):
        return "macro"
    if matcher.find_matches(text, ("listing", "delisting")):
        return "listing"
    if matcher.find_matches(text, ("exchange outage", "withdrawals suspended")):
        return "exchange"
    return "other"


def _is_high_urgency(text: str, matcher: KeywordMatcher) -> bool:
    return bool(matcher.find_matches(
        text,
        ("etf", "hack", "exploit", "exchange outage", "withdrawals suspended", "liquidation", "sec"),
    ))


def _asset_from_text(text: str, matcher: KeywordMatcher) -> Asset:
    if matcher.contains(text, "bitcoin") or matcher.contains(text, "btc"):
        return Asset.BTC
    if matcher.contains(text, "ethereum") or matcher.contains(text, "eth"):
        return Asset.ETH
    if matcher.find_matches(text, ("etf", "sec", "fed", "cpi", "crypto", "binance", "bybit", "coinbase")):
        return Asset.MARKET
    return Asset.OTHER
