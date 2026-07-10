from __future__ import annotations

from typing import Protocol

from app.models import NewsClassification, NewsItem, Sentiment


class NewsClassifier(Protocol):
    def classify(self, item: NewsItem) -> NewsClassification: ...


class MockNewsClassifier:
    """Deterministic classifier used for tests and local development."""

    def classify(self, item: NewsItem) -> NewsClassification:
        text = f"{item.title} {item.summary}".lower()
        bearish_words = {"ban", "hack", "lawsuit", "sell-off", "outflow"}
        bullish_words = {"adoption", "approval", "purchase", "inflow", "expands"}

        if any(word in text for word in bearish_words):
            sentiment, confidence = Sentiment.BEARISH, 0.85
        elif any(word in text for word in bullish_words):
            sentiment, confidence = Sentiment.BULLISH, 0.85
        else:
            sentiment, confidence = Sentiment.NEUTRAL, 0.5

        return NewsClassification(
            news_id=item.id,
            sentiment=sentiment,
            confidence=confidence,
            rationale="Deterministic keyword classification for Phase 1.",
            model_name="mock-keyword-v1",
        )
