"""Pydantic record contracts prepared for Phase 2 persistence.

These aliases keep the domain model as the single source of truth. SQL/ORM
mapping and migrations are deliberately deferred until Phase 2.
"""

from app.models import (
    BotEvent,
    ErrorRecord,
    NewsClassification,
    NewsItem,
    PaperOrder,
    PaperTrade,
    Position,
    RiskDecision,
    TradeSignal,
)

__all__ = [
    "BotEvent",
    "ErrorRecord",
    "NewsClassification",
    "NewsItem",
    "PaperOrder",
    "PaperTrade",
    "Position",
    "RiskDecision",
    "TradeSignal",
]
