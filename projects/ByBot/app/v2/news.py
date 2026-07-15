from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from time import monotonic
from typing import Any, Awaitable, Callable, Protocol
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


class EntityMapper:
    def symbols_for_text(self, text: str) -> tuple[Symbol, ...]:
        normalized = clean_news_text(text).casefold()
        matched = []
        for symbol, aliases in ALIASES.items():
            if any(re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized) for alias in aliases):
                matched.append(symbol)
        if matched:
            return tuple(matched)
        market_terms = ("crypto market", "federal reserve", "fed ", "cpi", "sec regulation")
        return tuple(Symbol) if any(term in normalized for term in market_terms) else ()

    def ecosystem_symbols(self, symbol: Symbol) -> tuple[Symbol, ...]:
        if symbol == Symbol.SOLUSDT:
            return (Symbol.SOLUSDT, Symbol.WIFUSDT, Symbol.BONKUSDT)
        return (symbol,)


class V2NewsAggregator:
    def __init__(
        self, sources: list[AsyncNewsSource], *, mapper: EntityMapper | None = None,
        max_age: timedelta = timedelta(hours=2), retries: int = 2,
    ) -> None:
        self.sources = sources
        self.mapper = mapper or EntityMapper()
        self.max_age = max_age
        self.retries = retries
        self.seen: set[str] = set()
        self.seen_urls: set[str] = set()
        self.health = {source.name: SourceState(source=source.name) for source in sources}
        self.duplicate_count = 0
        self.source_failures = 0

    async def poll(self) -> list[tuple[NewsItem, tuple[Symbol, ...], str]]:
        results = await asyncio.gather(
            *(self._fetch_isolated(source) for source in self.sources),
            return_exceptions=False,
        )
        accepted: list[tuple[NewsItem, tuple[Symbol, ...], str]] = []
        now = datetime.now(timezone.utc)
        for source, items in zip(self.sources, results):
            for item in items:
                if now - item.published_at > self.max_age:
                    continue
                normalized = item.model_copy(update={
                    "title": clean_news_text(item.title),
                    "summary": clean_news_text(item.summary),
                })
                symbols = self.mapper.symbols_for_text(f"{normalized.title} {normalized.summary}")
                fingerprint = semantic_fingerprint(normalized, symbols)
                normalized_url = (normalized.url or "").split("?", 1)[0].rstrip("/").casefold()
                if fingerprint in self.seen or (normalized_url and normalized_url in self.seen_urls):
                    self.duplicate_count += 1
                    continue
                self.seen.add(fingerprint)
                if normalized_url:
                    self.seen_urls.add(normalized_url)
                accepted.append((normalized, symbols, fingerprint))
        return accepted

    async def _fetch_isolated(self, source: AsyncNewsSource) -> list[NewsItem]:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                rows = await source.fetch()
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
            self.scores = scores; self.health = SourceHealth.OK; self.last_error = None
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
