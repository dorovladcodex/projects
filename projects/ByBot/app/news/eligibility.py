from __future__ import annotations

from dataclasses import dataclass

from app.models import (
    Asset,
    ClassificationStatus,
    NewsCategory,
    NewsClassification,
    Sentiment,
)


@dataclass(frozen=True)
class EligibilityResult:
    trade_eligible: bool
    reasons: list[str]


def calculate_trade_eligibility(
    *,
    classification_status: ClassificationStatus,
    sentiment: Sentiment,
    confidence: float,
    asset: Asset,
    category: str,
    error_code: str | None,
    minimum_confidence: float,
) -> EligibilityResult:
    reasons: list[str] = []
    if classification_status not in {
        ClassificationStatus.SUCCESS,
        ClassificationStatus.CACHE_HIT,
    }:
        reasons.append("classification status is not eligible")
    if classification_status == ClassificationStatus.FALLBACK_MOCK:
        reasons.append("mock fallback is not trade eligible")
    if sentiment == Sentiment.NEUTRAL:
        reasons.append("neutral sentiment")
    if confidence < minimum_confidence:
        reasons.append("confidence below minimum")
    if asset not in {Asset.BTC, Asset.ETH, Asset.MARKET}:
        reasons.append("unsupported asset")
    if category not in {item.value for item in NewsCategory}:
        reasons.append("invalid category")
    if error_code:
        reasons.append("classification has error")
    return EligibilityResult(trade_eligible=not reasons, reasons=reasons)


def apply_trade_eligibility(
    classification: NewsClassification,
    *,
    minimum_confidence: float,
) -> NewsClassification:
    result = calculate_trade_eligibility(
        classification_status=classification.classification_status,
        sentiment=classification.sentiment,
        confidence=classification.confidence,
        asset=classification.asset,
        category=classification.category,
        error_code=classification.error_code,
        minimum_confidence=minimum_confidence,
    )
    return classification.model_copy(
        update={
            "trade_eligible": result.trade_eligible,
            "eligibility_reasons": result.reasons,
        }
    )
