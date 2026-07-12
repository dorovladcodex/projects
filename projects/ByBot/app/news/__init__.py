"""News ingestion and classification boundaries."""
from app.news.classifier import (
    BaseNewsClassifier,
    LLMNewsClassifier,
    MockNewsClassifier,
    build_news_classifier,
)
from app.news.service import NewsService
from app.news.eligibility import apply_trade_eligibility, calculate_trade_eligibility
from app.news.sources import (
    BaseNewsSource,
    BybitAnnouncementsNewsSource,
    CryptoPanicNewsSource,
    GDELTNewsSource,
    RSSNewsSource,
)

__all__ = [
    "BaseNewsSource", "BybitAnnouncementsNewsSource", "CryptoPanicNewsSource",
    "GDELTNewsSource", "BaseNewsClassifier", "LLMNewsClassifier", "MockNewsClassifier",
    "NewsService", "RSSNewsSource", "build_news_classifier",
    "apply_trade_eligibility", "calculate_trade_eligibility",
]
