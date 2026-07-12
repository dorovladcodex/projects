from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from app.models import NewsItem
from app.news.text import clean_news_text


class BaseNewsSource(Protocol):
    name: str

    def fetch(self) -> list[NewsItem]: ...


class RSSNewsSource:
    """Small dependency-free RSS/Atom reader for public news feeds."""

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 8.0,
        fetcher: Callable[[str, float], bytes] | None = None,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.name = f"rss:{urlparse(url).netloc or url}"
        self._fetcher = fetcher or _fetch_url

    def fetch(self) -> list[NewsItem]:
        root = ElementTree.fromstring(self._fetcher(self.url, self.timeout_seconds))
        entries = root.findall(".//item")
        if not entries:
            entries = root.findall(".//{*}entry")
        return [self._to_news_item(entry) for entry in entries if self._title(entry)]

    def _to_news_item(self, entry: ElementTree.Element) -> NewsItem:
        title = self._title(entry)
        summary = clean_news_text(
            _text(entry, "description")
            or _text(entry, "{*}summary")
            or _text(entry, "{*}content")
            or "No summary provided."
        )
        link = _text(entry, "link") or ""
        if not link:
            link_element = entry.find("{*}link")
            link = link_element.get("href", "") if link_element is not None else ""
        published = _parse_date(
            _text(entry, "pubDate") or _text(entry, "{*}published") or _text(entry, "{*}updated")
        )
        return NewsItem(
            title=title,
            summary=summary[:1000],
            source=self.name,
            url=link or None,
            published_at=published,
        )

    @staticmethod
    def _title(entry: ElementTree.Element) -> str:
        return clean_news_text(_text(entry, "title") or _text(entry, "{*}title"))


class CryptoPanicNewsSource:
    """Placeholder for a future CryptoPanic adapter; intentionally does no I/O."""

    name = "cryptopanic-placeholder"

    def fetch(self) -> list[NewsItem]:
        return []


class GDELTNewsSource:
    """Placeholder for a future GDELT adapter; intentionally does no I/O."""

    name = "gdelt-placeholder"

    def fetch(self) -> list[NewsItem]:
        return []


class BybitAnnouncementsNewsSource:
    """Placeholder for a future Bybit announcements adapter; intentionally does no I/O."""

    name = "bybit-announcements-placeholder"

    def fetch(self) -> list[NewsItem]:
        return []


def _fetch_url(url: str, timeout_seconds: float) -> bytes:
    request = Request(url, headers={"User-Agent": "ByBot/0.1 news-reader"})
    with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - configured public RSS URL
        return response.read()


def _text(element: ElementTree.Element, path: str) -> str:
    found = element.find(path)
    return found.text.strip() if found is not None and found.text else ""


def _parse_date(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
