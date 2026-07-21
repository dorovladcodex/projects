from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from time import monotonic
from typing import Any, Awaitable, Callable, Mapping, Protocol
from urllib.request import Request, urlopen

from app.models import NewsItem, Symbol
from app.news.sources import RSSNewsSource
from app.news.text import clean_news_text
from app.v2.models import SourceHealth, SourceState


class AsyncNewsSource(Protocol):
    name: str
    reliability: float
    async def fetch(self) -> list[NewsItem]: ...


class AsyncRSSNewsSource:
    def __init__(
        self, name: str, url: str, *, timeout_seconds: float = 5,
        reliability: float = 0.8,
    ) -> None:
        self.name = name
        self.url = url
        self.reliability = reliability
        self._source = RSSNewsSource(url, timeout_seconds=timeout_seconds)

    async def fetch(self) -> list[NewsItem]:
        return await asyncio.wait_for(asyncio.to_thread(self._source.fetch), timeout=10)


class CoinGeckoSource:
    """Cached external trend/market source; no execution capability."""

    def __init__(
        self, endpoint: str, name: str, *, timeout_seconds: float = 5,
        cache_ttl_seconds: int = 60,
        http_get: Callable[[str, float], dict[str, Any]] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.name = name
        self.reliability = 0.75
        self.timeout_seconds = timeout_seconds
        self.cache_ttl_seconds = cache_ttl_seconds
        self._http_get = http_get or _json_get
        self._cache: tuple[float, list[dict[str, Any]]] | None = None

    async def fetch(self) -> list[dict[str, Any]]:
        if self._cache and monotonic() - self._cache[0] < self.cache_ttl_seconds:
            return list(self._cache[1])
        payload = await asyncio.wait_for(
            asyncio.to_thread(self._http_get, self.endpoint, self.timeout_seconds),
            timeout=self.timeout_seconds + 1,
        )
        rows = payload.get("coins") if isinstance(payload, dict) else payload
        result = list(rows or [])
        self._cache = (monotonic(), result)
        return result


class BybitAnnouncementSource:
    name = "bybit-announcements"
    reliability = 0.90

    def __init__(
        self, endpoint: str, *, timeout_seconds: float = 5,
        http_get: Callable[[str, float], dict[str, Any]] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self._http_get = http_get or _json_get

    async def fetch(self) -> list[NewsItem]:
        payload = await asyncio.wait_for(
            asyncio.to_thread(self._http_get, self.endpoint, self.timeout_seconds),
            timeout=self.timeout_seconds + 1,
        )
        rows = (payload.get("result") or {}).get("list") or []
        items: list[NewsItem] = []
        for row in rows:
            published_ms = int(row.get("dateTimestamp") or row.get("publishTime") or 0)
            published_at = (
                datetime.fromtimestamp(published_ms / 1000, tz=timezone.utc)
                if published_ms else datetime.now(timezone.utc)
            )
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            items.append(NewsItem(
                title=title, summary=str(row.get("description") or title),
                source=self.name, url=str(row.get("url") or "") or None,
                published_at=published_at,
                raw_category=str(row.get("type") or "announcement"),
            ))
        return items


ALIASES: dict[Symbol, tuple[str, ...]] = {
    Symbol.BTCUSDT: ("btc", "bitcoin"), Symbol.ETHUSDT: ("eth", "ethereum", "ether"),
    Symbol.SOLUSDT: ("sol", "solana"), Symbol.XRPUSDT: ("xrp", "ripple"),
    Symbol.DOGEUSDT: ("doge", "dogecoin"), Symbol.ADAUSDT: ("ada", "cardano"),
    Symbol.LINKUSDT: ("link", "chainlink"), Symbol.AVAXUSDT: ("avax", "avalanche"),
    Symbol.SUIUSDT: ("sui",), Symbol.NEARUSDT: ("near", "near protocol"),
    Symbol.LTCUSDT: ("ltc", "litecoin"), Symbol.TONUSDT: ("ton", "toncoin"),
    Symbol.PEPEUSDT: ("pepe",), Symbol.SHIBUSDT: ("shib", "shiba inu"),
    Symbol.WIFUSDT: ("wif", "dogwifhat"), Symbol.BONKUSDT: ("bonk",),
    Symbol.FLOKIUSDT: ("floki",),
}


@dataclass(frozen=True)
class EntityMatch:
    detected_entity: str
    matched_alias: str
    mapped_symbol: Symbol
    mapping_method: str
    confidence: float = 1.0


class EntityMapper:
    def __init__(
        self,
        aliases: Mapping[Symbol, tuple[str, ...]] | None = None,
        active_symbols: tuple[Symbol, ...] | None = None,
    ) -> None:
        self.aliases = dict(aliases or ALIASES)
        self.active_symbols = set(active_symbols) if active_symbols is not None else None

    def set_active_symbols(self, symbols: tuple[Symbol, ...]) -> None:
        self.active_symbols = set(symbols)

    def mappings_for_text(self, text: str) -> tuple[EntityMatch, ...]:
        original = clean_news_text(text)
        normalized = original.casefold()
        matches: list[EntityMatch] = []
        for symbol, aliases in self.aliases.items():
            if self.active_symbols is not None and symbol not in self.active_symbols:
                continue
            for alias in sorted(aliases, key=len, reverse=True):
                # NEAR is an ordinary English word. Accept the project phrase
                # case-insensitively, or the ticker only when explicitly uppercase.
                if symbol == Symbol.NEARUSDT and alias.casefold() == "near":
                    pattern = re.compile(r"(?<!\w)NEAR(?!\w)")
                    found = pattern.search(original)
                else:
                    pattern = re.compile(
                        rf"(?<!\w){re.escape(alias.casefold())}(?!\w)", re.IGNORECASE
                    )
                    found = pattern.search(normalized)
                if found is None:
                    continue
                matches.append(EntityMatch(
                    detected_entity=found.group(0), matched_alias=alias,
                    mapped_symbol=symbol,
                    mapping_method=("ticker_token" if alias.upper() == symbol.value.removesuffix("USDT") else "project_alias"),
                ))
                break
        return tuple(matches)

    def symbols_for_text(self, text: str) -> tuple[Symbol, ...]:
        normalized = clean_news_text(text).casefold()
        matched = [item.mapped_symbol for item in self.mappings_for_text(text)]
        if matched:
            return tuple(matched)
        market_terms = ("crypto market", "federal reserve", "fed ", "cpi", "sec regulation")
        market_symbols = tuple(Symbol)
        if self.active_symbols is not None:
            market_symbols = tuple(item for item in market_symbols if item in self.active_symbols)
        return market_symbols if any(term in normalized for term in market_terms) else ()

    def ecosystem_symbols(self, symbol: Symbol) -> tuple[Symbol, ...]:
        if symbol == Symbol.SOLUSDT:
            return (Symbol.SOLUSDT, Symbol.WIFUSDT, Symbol.BONKUSDT)
        return (symbol,)


class V2NewsAggregator:
    def __init__(
        self, sources: list[AsyncNewsSource], *, mapper: EntityMapper | None = None,
        max_age: timedelta = timedelta(hours=2), retries: int = 2,
        poll_interval_seconds: int = 180, run_id: str | None = None,
    ) -> None:
        self.sources = sources
        self.mapper = mapper or EntityMapper()
        self.max_age = max_age
        self.retries = retries
        self.poll_interval = timedelta(seconds=poll_interval_seconds)
        self._next_fetch_at: dict[int, datetime] = {}
        self.run_id = run_id
        self.seen: set[str] = set()
        self.seen_urls: set[str] = set()
        self._previous_identities: dict[str, dict[str, str | None]] = {}
        self._run_identities: dict[str, dict[str, str | None]] = {}
        self.health = {source.name: SourceState(source=source.name) for source in sources}
        self.duplicate_count = 0
        self.source_failures = 0
        self.items_received = 0
        self.items_accepted = 0
        self.items_rejected = 0
        self.unique_items_discovered = 0
        self.last_poll_audit: list[dict[str, Any]] = []
        self.source_metrics: dict[str, dict[str, int]] = {
            source.name: {
                "fetch_attempts": 0, "fetch_successes": 0,
                "fetch_failures": 0, "items_received": 0,
                "source_fetch_attempts": 0, "source_fetch_successes": 0,
                "source_fetch_failures": 0,
                "raw_feed_items_received": 0,
                "invalid_feed_items": 0,
                "duplicate_within_poll": 0,
                "duplicate_within_run": 0,
                "duplicate_from_previous_run": 0,
                "fresh_items": 0,
                "symbol_matched_items": 0,
                "unique_items_discovered": 0,
                "duplicate_items_seen": 0,
                "duplicate_items_not_reinserted": 0,
                "deterministic_filter_accepts": 0,
                "deterministic_filter_rejections": 0,
                "items_sent_to_llm": 0, "classified_items": 0,
                "llm_cache_hits": 0,
                "llm_budget_rejections": 0,
                "llm_circuit_breaker_rejections": 0,
                "classifier_failures": 0,
                "skipped_missing_keywords": 0,
                "skipped_low_importance": 0,
                "trade_eligible_items": 0, "candidates_generated": 0,
                "candidates_admitted": 0,
            }
            for source in sources
        }

    def restore_deduplication(self, items: list[NewsItem]) -> None:
        """Restore durable story identity before the first post-restart poll."""
        for item in items:
            normalized = item.model_copy(update={
                "title": clean_news_text(item.title),
                "summary": clean_news_text(item.summary),
            })
            symbols = self.mapper.symbols_for_text(
                f"{normalized.title} {normalized.summary} {normalized.raw_category or ''}"
            )
            fingerprint = semantic_fingerprint(normalized, symbols)
            self.seen.add(fingerprint)
            normalized_url = (normalized.url or "").split("?", 1)[0].rstrip("/").casefold()
            if normalized_url:
                self.seen_urls.add(normalized_url)
            identities = [f"content:{fingerprint}"]
            if normalized_url:
                identities.insert(0, f"url:{normalized_url}")
            for identity in identities:
                self._previous_identities[identity] = {
                    "first_seen_news_id": str(normalized.id),
                    "first_seen_run_id": None,
                }

    def restore_current_run_audits(self, audits: list[dict[str, Any]]) -> None:
        """Promote durable unique identities from this run after a restart."""
        for audit in audits:
            if audit.get("deduplication_status") != "unique":
                continue
            identities = list(audit.get("normalized_identities") or [])
            if not identities and audit.get("normalized_identity"):
                identities = [str(audit["normalized_identity"])]
            if not identities:
                continue
            for identity in identities:
                self._previous_identities.pop(identity, None)
                self._run_identities[identity] = {
                    "first_seen_news_id": str(audit.get("news_id") or "") or None,
                    "first_seen_run_id": self.run_id,
                }

    async def poll(
        self, *, now: datetime | None = None,
    ) -> list[tuple[NewsItem, tuple[Symbol, ...], str]]:
        self.last_poll_audit = []
        current = now or datetime.now(timezone.utc)
        eligible = [
            source for source in self.sources
            if current >= self._next_fetch_at.get(id(source), datetime.min.replace(tzinfo=timezone.utc))
        ]
        for source in eligible:
            self._next_fetch_at[id(source)] = current + self.poll_interval
        results = await asyncio.gather(
            *(self._fetch_isolated(source) for source in eligible),
            return_exceptions=False,
        )
        accepted: list[tuple[NewsItem, tuple[Symbol, ...], str]] = []
        for source, items in zip(eligible, results):
            poll_identities: set[str] = set()
            for raw_item in items:
                self.items_received += 1
                self.source_metrics[source.name]["items_received"] += 1
                self.source_metrics[source.name]["raw_feed_items_received"] += 1
                try:
                    item = (
                        raw_item
                        if isinstance(raw_item, NewsItem)
                        else NewsItem.model_validate(raw_item)
                    )
                except Exception as exc:
                    self.source_metrics[source.name]["invalid_feed_items"] += 1
                    self.last_poll_audit.append({
                        "source": source.name,
                        "deduplication_status": "invalid_feed_item",
                        "validation_error": type(exc).__name__,
                        "poll_time": current.isoformat(),
                    })
                    continue
                audit: dict[str, Any] = {
                    "news_id": str(item.id), "source": source.name,
                    "title": clean_news_text(item.title), "url": item.url,
                    "published_at": item.published_at.isoformat(),
                    "received_at": item.received_at.isoformat(),
                    "deduplication_status": "unique",
                    "detected_entities": [], "mapped_symbols": [],
                    "market_wide": False, "deterministic_filter_decision": "accepted",
                }
                normalized = item.model_copy(update={
                    "title": clean_news_text(item.title),
                    "summary": clean_news_text(item.summary),
                })
                combined_text = f"{normalized.title} {normalized.summary} {normalized.raw_category or ''}"
                mappings = self.mapper.mappings_for_text(combined_text)
                symbols = self.mapper.symbols_for_text(combined_text)
                audit["detected_entities"] = [item.detected_entity for item in mappings]
                audit["entity_mapping_evidence"] = [
                    {
                        "detected_entity": item.detected_entity,
                        "matched_alias": item.matched_alias,
                        "mapped_symbol": item.mapped_symbol.value,
                        "mapping_method": item.mapping_method,
                        "confidence": item.confidence,
                    }
                    for item in mappings
                ]
                audit["mapped_symbols"] = [symbol.value for symbol in symbols]
                audit["market_wide"] = len(symbols) > 1
                fingerprint = semantic_fingerprint(normalized, symbols)
                normalized_url = (normalized.url or "").split("?", 1)[0].rstrip("/").casefold()
                identities = [f"content:{fingerprint}"]
                if normalized_url:
                    identities.insert(0, f"url:{normalized_url}")
                identity = identities[0]
                duplicate_scope: str | None = None
                first_seen = None
                matching_identity = next(
                    (key for key in identities if key in poll_identities), None
                )
                if matching_identity is not None:
                    duplicate_scope = "within_poll"
                    first_seen = self._run_identities.get(matching_identity)
                elif (matching_identity := next(
                    (key for key in identities if key in self._run_identities), None
                )) is not None:
                    duplicate_scope = "within_run"
                    first_seen = self._run_identities.get(matching_identity)
                elif (matching_identity := next(
                    (key for key in identities if key in self._previous_identities), None
                )) is not None:
                    duplicate_scope = "previous_run"
                    first_seen = self._previous_identities.get(matching_identity)
                if duplicate_scope is not None:
                    self.duplicate_count += 1
                    self.source_metrics[source.name]["duplicate_items_seen"] += 1
                    self.source_metrics[source.name]["duplicate_items_not_reinserted"] += 1
                    scope_metric = {
                        "within_poll": "duplicate_within_poll",
                        "within_run": "duplicate_within_run",
                        "previous_run": "duplicate_from_previous_run",
                    }[duplicate_scope]
                    self.source_metrics[source.name][scope_metric] += 1
                    audit.update({
                        "deduplication_status": "duplicate",
                        "duplicate_scope": duplicate_scope,
                        "normalized_identity": matching_identity or identity,
                        "normalized_identities": identities,
                        "first_seen_news_id": (first_seen or {}).get("first_seen_news_id"),
                        "first_seen_run_id": (first_seen or {}).get("first_seen_run_id"),
                        "poll_time": current.isoformat(),
                        "deterministic_filter_decision": "not_evaluated_duplicate",
                    })
                    self.last_poll_audit.append(audit)
                    continue
                for identity_key in identities:
                    poll_identities.add(identity_key)
                    self._run_identities[identity_key] = {
                        "first_seen_news_id": str(normalized.id),
                        "first_seen_run_id": self.run_id,
                    }
                self.seen.add(fingerprint)
                if normalized_url:
                    self.seen_urls.add(normalized_url)
                self.unique_items_discovered += 1
                self.source_metrics[source.name]["unique_items_discovered"] += 1
                audit["normalized_identity"] = identity
                audit["normalized_identities"] = identities
                if current - normalized.published_at > self.max_age:
                    audit["deterministic_filter_decision"] = "old_news"
                    self.items_rejected += 1
                    self.source_metrics[source.name]["deterministic_filter_rejections"] += 1
                    self.last_poll_audit.append(audit)
                    continue
                self.source_metrics[source.name]["fresh_items"] += 1
                if not symbols:
                    audit["deterministic_filter_decision"] = "unrelated_asset"
                    self.items_rejected += 1
                    self.source_metrics[source.name]["deterministic_filter_rejections"] += 1
                    self.last_poll_audit.append(audit)
                    continue
                self.source_metrics[source.name]["symbol_matched_items"] += 1
                self.items_accepted += 1
                self.source_metrics[source.name]["deterministic_filter_accepts"] += 1
                self.last_poll_audit.append(audit)
                accepted.append((normalized, symbols, fingerprint))
        return accepted

    async def _fetch_isolated(self, source: AsyncNewsSource) -> list[NewsItem]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.source_metrics[source.name]["fetch_attempts"] += 1
            self.source_metrics[source.name]["source_fetch_attempts"] += 1
            try:
                rows = await source.fetch()
                self.source_metrics[source.name]["fetch_successes"] += 1
                self.source_metrics[source.name]["source_fetch_successes"] += 1
                state = self.health[source.name]
                state.health = SourceHealth.OK
                state.last_message_at = datetime.now(timezone.utc)
                state.last_error = None
                return rows
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(min(2 ** attempt, 4))
        self.source_failures += 1
        self.source_metrics[source.name]["fetch_failures"] += 1
        self.source_metrics[source.name]["source_fetch_failures"] += 1
        state = self.health[source.name]
        state.health = SourceHealth.UNAVAILABLE
        state.last_error = type(last_error).__name__ if last_error else "unknown"
        return []


class V2ExternalTrendService:
    """CoinGecko markets/trending isolation for MemeTrend; no LLM or execution."""

    def __init__(self, trending: CoinGeckoSource, markets: CoinGeckoSource) -> None:
        self.trending = trending; self.markets = markets
        self.scores: dict[Symbol, float] = {}
        self.health = SourceHealth.UNAVAILABLE
        self.last_error: str | None = None
        self.last_updated_at: datetime | None = None

    async def poll(self) -> None:
        try:
            trending_rows, market_rows = await asyncio.gather(
                self.trending.fetch(), self.markets.fetch()
            )
            scores: dict[Symbol, float] = {}
            for rank, row in enumerate(trending_rows):
                item = row.get("item") if isinstance(row, dict) else None
                symbol_text = str((item or row).get("symbol") or "").upper()
                for symbol in Symbol:
                    if symbol.value.removesuffix("USDT") == symbol_text:
                        scores[symbol] = max(scores.get(symbol, 0), 1.0 - rank / max(len(trending_rows), 1))
            for row in market_rows:
                symbol_text = str(row.get("symbol") or "").upper()
                change = float(row.get("price_change_percentage_24h") or 0)
                for symbol in Symbol:
                    if symbol.value.removesuffix("USDT") == symbol_text:
                        scores[symbol] = max(-1.0, min(1.0, scores.get(symbol, 0) + change / 100))
            self.scores = scores
            self.health = SourceHealth.OK
            self.last_error = None
            self.last_updated_at = datetime.now(timezone.utc)
        except Exception as exc:
            self.health = SourceHealth.DEGRADED; self.last_error = type(exc).__name__

    def score(self, symbol: Symbol) -> float:
        return self.scores.get(symbol, 0.0)


def semantic_fingerprint(item: NewsItem, symbols: tuple[Symbol, ...]) -> str:
    title = re.sub(r"[^a-z0-9]+", " ", clean_news_text(item.title).casefold()).strip()
    published = item.published_at.astimezone(timezone.utc)
    time_bucket = published.replace(
        minute=(published.minute // 30) * 30, second=0, microsecond=0
    ).isoformat()
    entities = ",".join(sorted(symbol.value for symbol in symbols))
    return sha256(f"{title}|{time_bucket}|{entities}".encode("utf-8")).hexdigest()


def build_default_news_sources(
    additional_urls: tuple[str, ...], *, announcement_url: str | None = None,
) -> list[AsyncNewsSource]:
    sources: list[AsyncNewsSource] = [
        AsyncRSSNewsSource("cointelegraph", "https://cointelegraph.com/rss", reliability=0.85),
        AsyncRSSNewsSource("decrypt", "https://decrypt.co/feed", reliability=0.80),
    ]
    known = {source.url for source in sources if isinstance(source, AsyncRSSNewsSource)}
    for index, url in enumerate(additional_urls):
        if url not in known:
            sources.append(AsyncRSSNewsSource(f"rss-{index + 1}", url, reliability=0.65))
    if announcement_url:
        sources.append(BybitAnnouncementSource(announcement_url))
    return sources


def _json_get(url: str, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "ByBot/2"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))
