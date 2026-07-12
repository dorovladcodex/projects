"""News ingestion and classification boundaries."""
from app.news.classifier import LLMNewsClassifier, MockNewsClassifier
from app.news.service import NewsService
from app.news.sources import (
    BaseNewsSource,
    BybitAnnouncementsNewsSource,
    CryptoPanicNewsSource,
    GDELTNewsSource,
    RSSNewsSource,
)

__all__ = [
    "BaseNewsSource", "BybitAnnouncementsNewsSource", "CryptoPanicNewsSource",
    "GDELTNewsSource", "LLMNewsClassifier", "MockNewsClassifier", "NewsService", "RSSNewsSource",
]
