from __future__ import annotations

from datetime import datetime, timedelta, timezone

import app.main as main_module
from app.models import Asset, NewsItem, Sentiment
from app.news import MockNewsClassifier, NewsService, RSSNewsSource
from app.news.keywords import KeywordMatcher
from app.news.service import detect_asset
from app.news.text import clean_news_text


def item(
    title: str = "Bitcoin ETF sees BlackRock inflow",
    summary: str = "A fresh BTC ETF inflow was reported.",
    *,
    published_at: datetime | None = None,
) -> NewsItem:
    return NewsItem(
        title=title,
        summary=summary,
        source="test-rss",
        url="https://example.invalid/item",
        published_at=published_at or datetime.now(timezone.utc),
    )


def service() -> NewsService:
    return NewsService([], MockNewsClassifier(), max_item_age=timedelta(minutes=60))


def test_duplicate_news_is_filtered_by_normalized_title() -> None:
    news_service = service()

    accepted, _, _ = news_service.ingest(item())
    duplicate, reason, _ = news_service.ingest(item("Bitcoin ETF: sees BlackRock inflow"))

    assert accepted is True
    assert duplicate is False
    assert reason == "duplicate"
    assert news_service.items_seen_count == 2
    assert news_service.items_filtered_count == 1


def test_old_news_is_not_sent_to_classifier() -> None:
    news_service = service()
    old_item = item(published_at=datetime.now(timezone.utc) - timedelta(minutes=61))

    accepted, reason, classification = news_service.ingest(old_item)

    assert accepted is False
    assert reason == "old_news"
    assert classification is None
    assert news_service.items_classified_count == 0
    assert news_service.mock_classifier_calls_count == 0


def test_keyword_relevance_and_asset_detection() -> None:
    news_service = service()
    irrelevant = item("Local sports team wins", "No financial relevance.")
    relevant = item("Ethereum exchange outage resolved", "Bybit reports an ETH service outage.")

    irrelevant_accepted, irrelevant_reason, _ = news_service.ingest(irrelevant)
    relevant_accepted, _, classification = news_service.ingest(relevant)

    assert irrelevant_accepted is False
    assert irrelevant_reason == "unrelated_asset"
    assert relevant_accepted is True
    assert relevant.asset_hint == Asset.ETH
    assert classification is not None


def test_detects_btc_eth_market_and_other_assets() -> None:
    assert detect_asset("Bitcoin ETF update") == Asset.BTC
    assert detect_asset("Ethereum upgrade update") == Asset.ETH
    assert detect_asset("Fed rate hike affects crypto") == Asset.MARKET
    assert detect_asset("Unrelated weather report") == Asset.OTHER


def test_mock_classifier_returns_strict_phase_4a_fields() -> None:
    classification = MockNewsClassifier().classify(
        item("Bitcoin hack triggers liquidation", "A BTC protocol security incident was reported.")
    )

    assert classification.asset == Asset.BTC
    assert classification.sentiment == Sentiment.BEARISH
    assert classification.confidence >= 0.7
    assert classification.category == "security"
    assert classification.urgency == "high"
    assert classification.reason


def test_sec_etf_news_is_important_and_accepted() -> None:
    news_service = service()

    accepted, reason, classification = news_service.ingest(
        item("SEC approves Bitcoin ETF", "BlackRock receives ETF approval for BTC.")
    )

    assert accepted is True
    assert reason == "accepted"
    assert classification is not None
    assert news_service.filtered_items[0].importance == 0.9
    assert {"sec", "etf", "blackrock", "approval"} <= set(news_service.last_filter_debug.matched_keywords)


def test_hack_or_exploit_news_is_accepted() -> None:
    news_service = service()

    accepted, _, classification = news_service.ingest(
        item("Ethereum protocol exploit reported", "A hack affects ETH liquidity.")
    )

    assert accepted is True
    assert classification is not None
    assert news_service.last_filter_debug.importance == 0.9
    assert "exploit" in news_service.last_filter_debug.matched_keywords


def test_medium_importance_news_can_be_rejected_by_configured_threshold() -> None:
    news_service = NewsService(
        [], MockNewsClassifier(), max_item_age=timedelta(minutes=60), min_importance_to_classify=0.8
    )

    accepted, reason, classification = news_service.ingest(
        item("Bitcoin whale moves treasury", "A BTC institutional treasury transfer was reported.")
    )

    assert accepted is False
    assert reason == "low_importance"
    assert classification is None
    assert news_service.last_filter_debug.rejection_reasons == ["low_importance"]


def test_encoding_cleanup_repairs_common_utf8_mojibake() -> None:
    assert clean_news_text("Hereâ€™s Pakistanâ€™s A16zâ€™s update") == "Here’s Pakistan’s A16z’s update"
    assert clean_news_text("Hereâs Pakistanâs A16zâs update") == "Here’s Pakistan’s A16z’s update"
    assert clean_news_text("â€œBitcoinâ€ â€” update") == "“Bitcoin” — update"
    assert clean_news_text("Already valid: Here’s Bitcoin — update") == "Already valid: Here’s Bitcoin — update"


def test_keyword_matcher_uses_whole_words_and_whole_phrases() -> None:
    matcher = KeywordMatcher()

    assert matcher.contains("SEC approves a Bitcoin ETF", "sec") is True
    assert matcher.contains("second scrutiny seized", "sec") is False
    assert matcher.contains("A ban was announced", "ban") is True
    assert matcher.contains("A bank reports earnings", "ban") is False
    assert matcher.contains("Exchange outage resolved", "exchange outage") is True
    assert matcher.contains("The exchange has an outage", "exchange outage") is False


def test_bullish_etf_approval_classification() -> None:
    classification = MockNewsClassifier().classify(
        item("SEC approves spot Bitcoin ETF from BlackRock", "Market update.")
    )

    assert classification.asset == Asset.BTC
    assert classification.sentiment == Sentiment.BULLISH
    assert classification.confidence >= 0.85
    assert classification.category == "etf"
    assert classification.urgency == "high"


def test_bearish_etf_rejection_and_exploit_classification() -> None:
    etf = MockNewsClassifier().classify(item("SEC rejects Bitcoin ETF", "ETF rejected after review."))
    exploit = MockNewsClassifier().classify(item("Ethereum exploit found", "A hack affects ETH users."))

    assert etf.sentiment == Sentiment.BEARISH
    assert etf.category == "etf"
    assert exploit.sentiment == Sentiment.BEARISH
    assert exploit.category == "security"


def test_normalized_titles_deduplicate_before_hashing() -> None:
    news_service = service()

    accepted, _, _ = news_service.ingest(item("SEC approves Bitcoin ETF — BlackRock"))
    duplicate, reason, _ = news_service.ingest(item("SEC approves Bitcoin ETF â€” BlackRock"))

    assert accepted is True
    assert duplicate is False
    assert reason == "duplicate"


def test_rss_source_parses_a_feed_without_network() -> None:
    payload = b"""<?xml version='1.0'?><rss><channel><item><title>Bitcoin ETF listing</title><description>BlackRock BTC ETF</description><link>https://example.invalid/a</link><pubDate>Tue, 12 Jul 2026 10:00:00 +0000</pubDate></item></channel></rss>"""
    source = RSSNewsSource("https://example.invalid/rss", fetcher=lambda _url, _timeout: payload)

    items = source.fetch()

    assert len(items) == 1
    assert items[0].title == "Bitcoin ETF listing"
    assert items[0].url == "https://example.invalid/a"


def test_news_endpoints_and_status_fields() -> None:
    previous = main_module.news_service
    try:
        main_module.news_service = service()
        accepted = main_module.news_test_item(item())
        all_news = main_module.news()
        filtered = main_module.filtered_news()
        classifications = main_module.news_classifications()
        debug = main_module.news_filter_debug()
        status = main_module.status()

        assert accepted["accepted"] is True
        assert len(all_news["items"]) == 1
        assert len(filtered["items"]) == 1
        assert len(classifications["classifications"]) == 1
        assert debug["items"][0]["accepted"] is True
        assert debug["items"][0]["importance"] == 0.9
        assert status["news_status"] == "OK"
        assert status["news_items_seen_count"] == 1
        assert status["news_items_filtered_count"] == 1
        assert status["items_classified_count"] == 1
        assert status["mock_classifier_calls_count"] == 1
        assert status["real_llm_calls_count"] == 0
        assert status["last_news_classification"]["asset"] == "BTC"
    finally:
        main_module.news_service = previous
