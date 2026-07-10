from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from statistics import pstdev
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import MarketDataProviderName, Settings
from app.models import MarketSnapshot, SimpleTrend, Symbol


class MarketDataProvider(Protocol):
    def get_snapshot(self, symbol: Symbol) -> MarketSnapshot: ...


HttpGet = Callable[[str, float], dict[str, Any]]


def _default_http_get(url: str, timeout: float) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "ByBot/0.2 DATA_ONLY"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Bybit returned a non-object response")
    return data


class BybitRestMarketDataClient:
    """Public Bybit V5 ticker client. It never sends orders or auth headers."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.bybit.com",
        timeout_seconds: float = 5.0,
        http_get: HttpGet | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get or _default_http_get

    def get_snapshot(self, symbol: Symbol) -> MarketSnapshot:
        query = urlencode({"category": "linear", "symbol": symbol.value})
        data = self.http_get(
            f"{self.base_url}/v5/market/tickers?{query}",
            self.timeout_seconds,
        )
        return self._parse_ticker(symbol, data)

    def _parse_ticker(self, symbol: Symbol, data: dict[str, Any]) -> MarketSnapshot:
        if data.get("retCode") != 0:
            message = data.get("retMsg", "unknown Bybit error")
            raise ValueError(f"Bybit ticker request failed: {message}")

        result = data.get("result")
        if not isinstance(result, dict):
            raise ValueError("Bybit ticker response is missing result")
        tickers = result.get("list")
        if not isinstance(tickers, list) or not tickers:
            raise ValueError(f"Bybit ticker response has no data for {symbol.value}")

        raw = tickers[0]
        if not isinstance(raw, dict):
            raise ValueError("Bybit ticker item is not an object")

        ticker_symbol = raw.get("symbol")
        if ticker_symbol != symbol.value:
            raise ValueError(f"Bybit returned unexpected symbol: {ticker_symbol}")

        last_price = _to_float(raw, "lastPrice")
        bid_price = _to_float(raw, "bid1Price")
        ask_price = _to_float(raw, "ask1Price")
        volume_24h = _to_optional_float(raw, "volume24h")

        return MarketSnapshot(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            last_price=last_price,
            bid_price=bid_price,
            ask_price=ask_price,
            trend_score=0.0,
            volatility_pct=0.0,
            liquidity_ok=bid_price > 0 and ask_price > bid_price,
            api_stable=True,
            volume_24h=volume_24h,
        )


class MockMarketDataProvider:
    def get_snapshot(self, symbol: Symbol) -> MarketSnapshot:
        price = 60_000.0 if symbol == Symbol.BTCUSDT else 3_000.0
        return MarketSnapshot(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            last_price=price,
            bid_price=price - 1,
            ask_price=price + 1,
            trend_score=0.65,
            volatility_pct=2.0,
            liquidity_ok=True,
            api_stable=True,
            price_change_1m_pct=0.25,
            volume_24h=25_000.0 if symbol == Symbol.BTCUSDT else 400_000.0,
        )


class MarketDataService:
    def __init__(
        self,
        provider: MarketDataProvider,
        symbols: Iterable[Symbol],
        *,
        history_limit: int = 120,
    ) -> None:
        self.provider = provider
        self.symbols = tuple(symbols)
        self.history_limit = history_limit
        self.history: dict[Symbol, list[MarketSnapshot]] = defaultdict(list)
        self.status = "INITIALIZING"
        self.last_error: str | None = None
        self.last_updated: datetime | None = None

    def refresh_all(self) -> None:
        refreshed = 0
        errors: list[str] = []
        for symbol in self.symbols:
            try:
                snapshot = self.provider.get_snapshot(symbol)
                self._append(self._with_derived_metrics(snapshot))
                refreshed += 1
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                errors.append(f"{symbol.value}: {exc}")

        if refreshed == len(self.symbols):
            self.status = "OK"
            self.last_error = None
            self.last_updated = datetime.now(timezone.utc)
            return

        self.status = "DATA_UNAVAILABLE"
        self.last_error = "; ".join(errors) if errors else "No market data refreshed"
        if refreshed:
            self.last_updated = datetime.now(timezone.utc)

    def latest_snapshots(self) -> list[MarketSnapshot]:
        return [items[-1] for items in self.history.values() if items]

    def latest_snapshot(self, symbol: Symbol) -> MarketSnapshot | None:
        items = self.history.get(symbol, [])
        return items[-1] if items else None

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "last_error": self.last_error,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "snapshots": [snapshot_to_payload(snapshot) for snapshot in self.latest_snapshots()],
        }

    def _append(self, snapshot: MarketSnapshot) -> None:
        items = self.history[snapshot.symbol]
        items.append(snapshot)
        if len(items) > self.history_limit:
            del items[: len(items) - self.history_limit]

    def _with_derived_metrics(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        prior = self.history[snapshot.symbol]
        price_change_1m_pct = _price_change_since(prior, snapshot, timedelta(minutes=1))
        volatility_pct = _simple_volatility_pct([*prior, snapshot])
        trend_score = _trend_score(price_change_1m_pct)
        simple_trend = _simple_trend(price_change_1m_pct)
        return snapshot.model_copy(
            update={
                "price_change_1m_pct": price_change_1m_pct,
                "simple_trend": simple_trend,
                "simple_volatility": volatility_pct,
                "volatility_pct": volatility_pct,
                "trend_score": trend_score,
            }
        )


def build_market_data_service(settings: Settings) -> MarketDataService:
    symbols = tuple(Symbol(symbol) for symbol in settings.allowed_symbols)
    if settings.market_data_provider == MarketDataProviderName.BYBIT_REST:
        provider: MarketDataProvider = BybitRestMarketDataClient(
            base_url=settings.bybit_public_base_url,
            timeout_seconds=settings.market_data_timeout_seconds,
        )
    else:
        provider = MockMarketDataProvider()

    return MarketDataService(
        provider,
        symbols,
        history_limit=settings.market_data_history_limit,
    )


def snapshot_to_payload(snapshot: MarketSnapshot) -> dict[str, Any]:
    return {
        "symbol": snapshot.symbol.value,
        "last_price": snapshot.last_price,
        "bid_price": snapshot.bid_price,
        "ask_price": snapshot.ask_price,
        "spread": snapshot.spread,
        "spread_pct": snapshot.spread_pct,
        "simple_trend": snapshot.simple_trend.value,
        "simple_volatility": snapshot.simple_volatility,
        "volume_24h": snapshot.volume_24h,
        "timestamp": snapshot.timestamp.isoformat(),
        # Backward-compatible dashboard aliases.
        "price": snapshot.last_price,
        "bid": snapshot.bid_price,
        "ask": snapshot.ask_price,
        "spread_bps": snapshot.spread_bps,
        "price_change_1m_pct": snapshot.price_change_1m_pct,
        "volatility_pct": snapshot.volatility_pct,
        "trend_score": snapshot.trend_score,
        "trend_direction": snapshot.simple_trend.value.upper(),
    }


def _to_float(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit ticker field {key} is not numeric") from exc
    if parsed <= 0:
        raise ValueError(f"Bybit ticker field {key} must be positive")
    return parsed


def _to_optional_float(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit ticker field {key} is not numeric") from exc
    return parsed if parsed >= 0 else None


def _price_change_since(
    prior: list[MarketSnapshot],
    current: MarketSnapshot,
    window: timedelta,
) -> float:
    cutoff = current.timestamp - window
    baseline = next((item for item in prior if item.timestamp >= cutoff), None)
    if baseline is None and prior:
        baseline = prior[0]
    if baseline is None:
        return 0.0
    return (current.last_price - baseline.last_price) / baseline.last_price * 100


def _simple_volatility_pct(items: list[MarketSnapshot]) -> float:
    if len(items) < 2:
        return 0.0
    returns = []
    for previous, current in zip(items, items[1:]):
        returns.append(math.log(current.last_price / previous.last_price))
    if len(returns) < 2:
        return abs(returns[0]) * 100
    return pstdev(returns) * 100


def _trend_score(price_change_1m_pct: float) -> float:
    if price_change_1m_pct == 0:
        return 0.0
    return max(-1.0, min(1.0, price_change_1m_pct / 0.5))


def _simple_trend(price_change_1m_pct: float) -> SimpleTrend:
    if price_change_1m_pct >= 0.05:
        return SimpleTrend.BULLISH
    if price_change_1m_pct <= -0.05:
        return SimpleTrend.BEARISH
    return SimpleTrend.SIDEWAYS
