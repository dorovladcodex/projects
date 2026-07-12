from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re

from app.models import Asset, NewsClassification, NewsFilterDebug, NewsItem
from app.news.classifier import LLMNewsClassifier
from app.news.keywords import KeywordMatcher
from app.news.sources import BaseNewsSource
from app.news.text import clean_news_text


HIGH_IMPORTANCE_KEYWORDS = (
    "exchange outage", "clarity act", "blackrock", "liquidation", "delisting",
    "investigation", "exploit", "lawsuit", "listing", "approval", "charges",
    "binance", "coinbase", "bybit", "fraud", "mica", "hack", "etf", "sec",
    "fed", "cpi", "ban",
)
MEDIUM_IMPORTANCE_KEYWORDS = (
    "stablecoin regulation", "bitcoin bonds", "institutional", "tokenization",
    "treasury", "whale", "occ",
)
MARKET_KEYWORDS = set(HIGH_IMPORTANCE_KEYWORDS) | set(MEDIUM_IMPORTANCE_KEYWORDS) | {
    "bitcoin", "btc", "ethereum", "eth", "crypto", "rate cut", "rate hike",
}
KEYWORD_MATCHER = KeywordMatcher()


class NewsService:
    def __init__(
        self,
        sources: list[BaseNewsSource],
        classifier: LLMNewsClassifier | None,
        *,
        max_item_age: timedelta,
        min_importance_to_classify: float = 0.3,
    ) -> None:
        self.sources = sources
        self.classifier = classifier
        self.max_item_age = max_item_age
        self.min_importance_to_classify = min_importance_to_classify
        self.items: list[NewsItem] = []
        self.filtered_items: list[NewsItem] = []
        self.classifications: list[NewsClassification] = []
        self.filter_debug: list[NewsFilterDebug] = []
        self._title_hashes: set[str] = set()
        self._source_title_hashes: set[str] = set()
        self.last_news_item: NewsItem | None = None
        self.last_filtered_news_item: NewsItem | None = None
        self.last_news_classification: NewsClassification | None = None
        self.last_filter_debug: NewsFilterDebug | None = None
        self.items_seen_count = 0
        self.items_filtered_count = 0
        self.items_classified_count = 0
        self.mock_classifier_calls_count = 0
        self.real_llm_calls_count = 0
        self.llm_cache_hits = 0
        self.estimated_input_tokens = 0
        self.estimated_output_tokens = 0
        self.last_error: str | None = None
        self.last_polled_at: datetime | None = None

    @property
    def status(self) -> str:
        if self.last_error and not self.last_polled_at:
            return "ERROR"
        return "OK" if self.last_error is None else "DEGRADED"

    def poll(self) -> None:
        errors: list[str] = []
        for source in self.sources:
            try:
                for item in source.fetch():
                    self.ingest(item)
            except Exception as exc:  # source errors must never take down the app
                errors.append(f"{source.name}: {exc}")
        self.last_error = "; ".join(errors) if errors else None
        self.last_polled_at = datetime.now(timezone.utc)

    def ingest(
        self, item: NewsItem, *, now: datetime | None = None
    ) -> tuple[bool, str, NewsClassification | None]:
        now = now or datetime.now(timezone.utc)
        self.items_seen_count += 1
        normalized = normalize_item(item)
        self.last_news_item = normalized
        matched_keywords = matched_importance_keywords(normalized)
        title_hash = normalized_title_hash(normalized.title)
        source_title_hash = source_title_hash_for(normalized.source, normalized.title)

        def finish(accepted: bool, code: str, classification: NewsClassification | None = None) -> tuple[bool, str, NewsClassification | None]:
            debug = NewsFilterDebug(
                news_id=normalized.id,
                title=normalized.title,
                asset_hint=normalized.asset_hint,
                importance=normalized.importance,
                matched_keywords=matched_keywords,
                accepted=accepted,
                rejection_reasons=[] if accepted else [code],
            )
            self.filter_debug.append(debug)
            self.last_filter_debug = debug
            return accepted, code, classification

        if title_hash in self._title_hashes or source_title_hash in self._source_title_hashes:
            return finish(False, "duplicate")
        self._title_hashes.add(title_hash)
        self._source_title_hashes.add(source_title_hash)
        self.items.append(normalized)
        if now - normalized.published_at > self.max_item_age:
            return finish(False, "old_news")
        if normalized.asset_hint == Asset.OTHER:
            return finish(False, "unrelated_asset")
        if not matched_keywords:
            return finish(False, "missing_keywords")
        if normalized.importance < self.min_importance_to_classify:
            return finish(False, "low_importance")

        self.filtered_items.append(normalized)
        self.last_filtered_news_item = normalized
        self.items_filtered_count += 1
        if self.classifier is None:
            return finish(True, "accepted")
        classification = self.classifier.classify(normalized)
        self.classifications.append(classification)
        self.last_news_classification = classification
        self.items_classified_count += 1
        if classification.model_name.startswith("mock-"):
            self.mock_classifier_calls_count += 1
        else:
            self.real_llm_calls_count += 1
        compact_input = " ".join(
            (normalized.title, normalized.summary, normalized.source, normalized.published_at.isoformat(), normalized.asset_hint.value)
        )
        self.estimated_input_tokens += _estimate_tokens(compact_input)
        self.estimated_output_tokens += _estimate_tokens(classification.model_dump_json())
        return finish(True, "accepted", classification)

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "last_error": self.last_error,
            "last_polled_at": self.last_polled_at.isoformat() if self.last_polled_at else None,
            "items": [item.model_dump(mode="json") for item in self.items],
            "items_seen_count": self.items_seen_count,
        }

    def filter_debug_payload(self) -> list[dict[str, object]]:
        return [item.model_dump(mode="json") for item in self.filter_debug[-100:]]


def normalize_item(item: NewsItem) -> NewsItem:
    item.title = clean_news_text(item.title)
    item.summary = clean_news_text(item.summary)
    item.asset_hint = detect_asset(f"{item.title} {item.summary}")
    item.importance = score_importance(item)
    return item


def detect_asset(text: str) -> Asset:
    normalized = clean_news_text(text)
    if KEYWORD_MATCHER.contains(normalized, "bitcoin") or KEYWORD_MATCHER.contains(normalized, "btc"):
        return Asset.BTC
    if KEYWORD_MATCHER.contains(normalized, "ethereum") or KEYWORD_MATCHER.contains(normalized, "eth"):
        return Asset.ETH
    if KEYWORD_MATCHER.find_matches(normalized, MARKET_KEYWORDS):
        return Asset.MARKET
    return Asset.OTHER


def matched_importance_keywords(item: NewsItem) -> list[str]:
    text = clean_news_text(f"{item.title} {item.summary}")
    return KEYWORD_MATCHER.find_matches(text, (*HIGH_IMPORTANCE_KEYWORDS, *MEDIUM_IMPORTANCE_KEYWORDS))


def score_importance(item: NewsItem) -> float:
    keywords = matched_importance_keywords(item)
    if any(keyword in HIGH_IMPORTANCE_KEYWORDS for keyword in keywords):
        return 0.9
    if keywords:
        return 0.5
    return 0.0


def normalized_title_hash(title: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", clean_news_text(title).lower())
    return sha256(normalized.encode("utf-8")).hexdigest()


def source_title_hash_for(source: str, title: str) -> str:
    return sha256(f"{source.lower()}:{normalized_title_hash(title)}".encode("utf-8")).hexdigest()


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)
