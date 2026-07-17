from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from statistics import pstdev
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import websockets

from app.config import Settings
from app.models import Symbol
from app.v2.models import MarketFeatureSnapshot, SourceHealth, SourceState


WINDOWS = {
    "10s": 10, "30s": 30, "1m": 60, "3m": 180,
    "5m": 300, "15m": 900, "1h": 3600,
}


@dataclass(frozen=True)
class TradePoint:
    timestamp: datetime
    price: Decimal
    quantity: Decimal
    side: str


@dataclass(frozen=True)
class LiquidationPoint:
    timestamp: datetime
    notional: Decimal
    side: str


class RollingFeatureEngine:
    """Deterministic per-symbol numeric windows fed by WS or tests."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        limit = settings.v2_feature_history_limit
        self.trades: dict[Symbol, deque[TradePoint]] = defaultdict(
            lambda: deque(maxlen=limit)
        )
        self.liquidations: dict[Symbol, deque[LiquidationPoint]] = defaultdict(
            lambda: deque(maxlen=limit)
        )
        self.books: dict[Symbol, tuple[Decimal, Decimal, Decimal, Decimal, datetime]] = {}
        self._book_levels: dict[
            Symbol, tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]
        ] = {}
        self.tickers: dict[Symbol, dict[str, Any]] = {}
        self.funding: dict[Symbol, tuple[Decimal, datetime]] = {}
        self.open_interest: dict[Symbol, deque[tuple[datetime, Decimal]]] = defaultdict(
            lambda: deque(maxlen=limit)
        )
        self.source_states = {
            name: SourceState(source=name)
            for name in ("ticker", "trades", "orderbook", "liquidations", "rest")
        }
        self.stale_incidents = 0  # Backward-compatible critical incident counter.
        self.stale_feature_observations = 0
        self.invalid_liquidation_symbols: set[Symbol] = set()
        self._source_age_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=10_000)
        )

    def ingest_ticker(self, symbol: Symbol, data: dict[str, Any], timestamp: datetime) -> None:
        # Bybit ticker messages after the initial snapshot are sparse deltas.
        # Merge them so a funding-only delta cannot erase lastPrice.
        ticker = self.tickers.setdefault(symbol, {})
        ticker.update(data)
        ticker["timestamp"] = timestamp
        self._healthy("ticker", timestamp)

    def ingest_trade(
        self, symbol: Symbol, price: Decimal, quantity: Decimal,
        side: str, timestamp: datetime,
    ) -> None:
        self.trades[symbol].append(TradePoint(timestamp, price, quantity, side.upper()))
        self._healthy("trades", timestamp)

    def ingest_orderbook(
        self, symbol: Symbol, bids: list[list[object]], asks: list[list[object]],
        timestamp: datetime, *, snapshot: bool = True,
    ) -> None:
        if snapshot or symbol not in self._book_levels:
            bid_levels: dict[Decimal, Decimal] = {}
            ask_levels: dict[Decimal, Decimal] = {}
        else:
            current_bids, current_asks = self._book_levels[symbol]
            bid_levels = dict(current_bids)
            ask_levels = dict(current_asks)
        _apply_book_updates(bid_levels, bids)
        _apply_book_updates(ask_levels, asks)
        self._book_levels[symbol] = (bid_levels, ask_levels)
        if not bid_levels or not ask_levels:
            return
        bid = max(bid_levels); ask = min(ask_levels)
        if bid <= 0 or ask <= bid:
            return
        bid_depth = sum((price * quantity for price, quantity in bid_levels.items()), Decimal("0"))
        ask_depth = sum((price * quantity for price, quantity in ask_levels.items()), Decimal("0"))
        self.books[symbol] = (bid, ask, bid_depth, ask_depth, timestamp)
        self._healthy("orderbook", timestamp)

    def ingest_liquidation(
        self, symbol: Symbol, side: str, price: Decimal,
        quantity: Decimal, timestamp: datetime,
    ) -> None:
        if side.upper() not in {"BUY", "SELL"} or price <= 0 or quantity <= 0:
            self.invalid_liquidation_symbols.add(symbol)
            state = self.source_states["liquidations"]
            state.health = SourceHealth.DEGRADED
            state.last_error = "invalid liquidation payload"
            return
        self.invalid_liquidation_symbols.discard(symbol)
        self.liquidations[symbol].append(
            LiquidationPoint(timestamp, price * quantity, side.upper())
        )
        self._healthy("liquidations", timestamp)

    def ingest_rest_metrics(
        self, symbol: Symbol, *, funding_rate: Decimal | None,
        open_interest: Decimal | None, volume_24h: Decimal | None,
        timestamp: datetime,
    ) -> None:
        if funding_rate is not None:
            self.funding[symbol] = (funding_rate, timestamp)
        if open_interest is not None:
            self.open_interest[symbol].append((timestamp, open_interest))
        ticker = self.tickers.setdefault(symbol, {})
        if volume_24h is not None:
            ticker["volume24h"] = volume_24h
        self._healthy("rest", timestamp)

    def snapshot(
        self, symbol: Symbol, *, now: datetime | None = None,
        btc_snapshot: MarketFeatureSnapshot | None = None,
    ) -> MarketFeatureSnapshot | None:
        current = now or datetime.now(timezone.utc)
        ticker = self.tickers.get(symbol)
        book = self.books.get(symbol)
        if not ticker or not book:
            return None
        last = _dec(ticker.get("lastPrice") or ticker.get("price"))
        bid, ask, bid_depth, ask_depth, book_time = book
        ticker_time = ticker.get("timestamp")
        trade_time = self.trades[symbol][-1].timestamp if self.trades[symbol] else None
        liquidation_time = (
            self.liquidations[symbol][-1].timestamp
            if self.liquidations[symbol] else None
        )
        source_timestamps = {
            "ticker": ticker_time if isinstance(ticker_time, datetime) else None,
            "orderbook": book_time,
            "trades": trade_time,
            "liquidations": liquidation_time,
        }
        source_ages = {
            source: max(0.0, (current - timestamp).total_seconds())
            if timestamp is not None else None
            for source, timestamp in source_timestamps.items()
        }
        for source, age in source_ages.items():
            if age is not None:
                self._source_age_samples[source].append(age)
        mandatory_times = [item for item in (ticker_time, book_time) if isinstance(item, datetime)]
        stale_reasons: list[str] = []
        stale_evidence: list[dict[str, Any]] = []
        if len(mandatory_times) != 2:
            stale_reasons.append("mandatory source timestamp missing")
        elif any(
            (current - item).total_seconds() > self.settings.v2_market_stale_seconds
            for item in mandatory_times
        ):
            stale_reasons.append("ticker or orderbook is stale")
        if not self.trades[symbol]:
            stale_reasons.append("public trades are unavailable")
        elif (current - self.trades[symbol][-1].timestamp).total_seconds() > (
            self.settings.v2_market_stale_seconds
        ):
            stale_reasons.append("public trades are stale")
        for source in ("ticker", "orderbook", "trades"):
            age = source_ages[source]
            if age is None or age > self.settings.v2_market_stale_seconds:
                stale_evidence.append({
                    "source": source,
                    "observed_age_seconds": age,
                    "configured_maximum_age_seconds": self.settings.v2_market_stale_seconds,
                    "latest_source_timestamp": (
                        source_timestamps[source].isoformat()
                        if source_timestamps[source] else None
                    ),
                    "evaluation_timestamp": current.isoformat(),
                })
        fresh = not stale_reasons
        if not fresh:
            self.stale_feature_observations += 1
        momentum: dict[str, Decimal] = {}
        breakout: dict[str, Decimal] = {}
        acceleration: dict[str, Decimal] = {}
        imbalance: dict[str, Decimal] = {}
        volatility: dict[str, Decimal] = {}
        for label, seconds in WINDOWS.items():
            window = [p for p in self.trades[symbol] if current - p.timestamp <= timedelta(seconds=seconds)]
            momentum[label] = _momentum(window, last)
            breakout[label] = _breakout_distance(window, last)
            acceleration[label] = _volume_acceleration(window, current, seconds)
            imbalance[label] = _trade_imbalance(window)
            volatility[label] = _volatility(window)
        book_total = bid_depth + ask_depth
        book_imbalance = (
            (bid_depth - ask_depth) / book_total if book_total > 0 else Decimal("0")
        )
        spread_bps = (ask - bid) / ((ask + bid) / 2) * Decimal("10000")
        prices = [point.price for point in self.trades[symbol] if current - point.timestamp <= timedelta(minutes=15)]
        local_high = max(prices, default=last); local_low = min(prices, default=last)
        liqs = [point for point in self.liquidations[symbol] if current - point.timestamp <= timedelta(minutes=5)]
        # A Bybit liquidation Buy closes a short; Sell closes a long.
        short_liq = sum((p.notional for p in liqs if p.side == "BUY"), Decimal("0"))
        long_liq = sum((p.notional for p in liqs if p.side == "SELL"), Decimal("0"))
        liq_total = short_liq + long_liq
        oi_change = _series_change(self.open_interest[symbol], current, timedelta(minutes=5))
        funding_rate = self.funding.get(symbol, (None, current))[0]
        funding_deviation = funding_rate * Decimal("10000") if funding_rate is not None else None
        relative = Decimal("0")
        if btc_snapshot and symbol != Symbol.BTCUSDT:
            relative = momentum["5m"] - btc_snapshot.price_momentum.get("5m", Decimal("0"))
        market_regime = _regime(momentum["15m"], volatility["15m"])
        return MarketFeatureSnapshot(
            symbol=symbol, timestamp=current, fresh=fresh, stale_reasons=stale_reasons,
            last_price=last, bid_price=bid, ask_price=ask, spread_bps=spread_bps,
            bid_depth_usdt=bid_depth, ask_depth_usdt=ask_depth,
            price_momentum=momentum, breakout_distance_bps=breakout,
            volume_acceleration=acceleration, trade_imbalance=imbalance,
            orderbook_imbalance=book_imbalance, realized_volatility=volatility,
            atr_bps=_atr_bps(prices, last),
            distance_from_high_bps=(last - local_high) / last * Decimal("10000"),
            distance_from_low_bps=(last - local_low) / last * Decimal("10000"),
            relative_strength_vs_btc_bps=relative, funding_rate=funding_rate,
            funding_deviation_bps=funding_deviation,
            open_interest=self.open_interest[symbol][-1][1] if self.open_interest[symbol] else None,
            open_interest_change_pct=oi_change,
            liquidation_long_usdt=long_liq, liquidation_short_usdt=short_liq,
            liquidation_imbalance=(short_liq - long_liq) / liq_total if liq_total else Decimal("0"),
            volume_24h=_dec(ticker.get("volume24h"), "0"), market_regime=market_regime,
            source_health={key: value.health for key, value in self.source_states.items()},
            source_timestamps=source_timestamps,
            source_age_seconds=source_ages,
            stale_evidence=stale_evidence,
            liquidation_last_valid_at=liquidation_time,
            liquidation_data_age_seconds=source_ages["liquidations"],
            liquidation_data_valid=symbol not in self.invalid_liquidation_symbols,
            liquidation_feed_initialized=liquidation_time is not None,
            liquidation_feed_available=(
                self.source_states["liquidations"].health
                not in {SourceHealth.UNAVAILABLE, SourceHealth.DEGRADED}
            ),
        )

    def record_critical_stale_incident(self) -> None:
        self.stale_incidents += 1

    def data_age_metrics(self) -> dict[str, dict[str, float | None]]:
        result: dict[str, dict[str, float | None]] = {}
        for source in ("ticker", "trades", "orderbook", "liquidations", "rest"):
            samples = sorted(self._source_age_samples.get(source, ()))
            state = self.source_states[source]
            latest_age = (
                max(0.0, (datetime.now(timezone.utc) - state.last_message_at).total_seconds())
                if state.last_message_at else None
            )
            result[source] = {
                "maximum": max(samples) if samples else None,
                "p50": _percentile(samples, 0.50),
                "p95": _percentile(samples, 0.95),
                "latest_message_age": latest_age,
            }
        return result

    def _healthy(self, source: str, timestamp: datetime) -> None:
        state = self.source_states[source]
        state.health = SourceHealth.OK
        state.last_message_at = timestamp
        state.last_error = None


class BybitPublicWebSocketEngine:
    """Resilient public WS pump. One failed topic never enables execution."""

    def __init__(self, settings: Settings, features: RollingFeatureEngine) -> None:
        self.settings = settings
        self.features = features
        self.running = False
        self.reconnects = 0

    async def run(self, symbols: tuple[Symbol, ...]) -> None:
        self.running = True
        delay = 1
        while self.running:
            try:
                async with websockets.connect(
                    self.settings.v2_public_ws_url, open_timeout=10, ping_interval=20,
                ) as socket:
                    topics = [
                        topic for symbol in symbols for topic in (
                            f"tickers.{symbol.value}", f"publicTrade.{symbol.value}",
                            f"orderbook.50.{symbol.value}", f"allLiquidation.{symbol.value}",
                        )
                    ]
                    await socket.send(json.dumps({"op": "subscribe", "args": topics}))
                    delay = 1
                    async for raw in socket:
                        self.handle_message(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.reconnects += 1
                for state in self.features.source_states.values():
                    state.health = SourceHealth.DEGRADED
                    state.last_error = type(exc).__name__
                    state.reconnects += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.settings.v2_ws_reconnect_max_seconds)

    def stop(self) -> None:
        self.running = False

    def handle_message(self, message: dict[str, Any]) -> None:
        topic = str(message.get("topic") or "")
        data = message.get("data")
        if not topic or data is None:
            return
        symbol_value = topic.rsplit(".", 1)[-1]
        try:
            symbol = Symbol(symbol_value)
        except ValueError:
            return
        timestamp = datetime.fromtimestamp(
            int(message.get("ts") or 0) / 1000, tz=timezone.utc
        ) if message.get("ts") else datetime.now(timezone.utc)
        rows = data if isinstance(data, list) else [data]
        if topic.startswith("tickers.") and isinstance(rows[0], dict):
            self.features.ingest_ticker(symbol, rows[0], timestamp)
        elif topic.startswith("publicTrade."):
            for row in rows:
                self.features.ingest_trade(
                    symbol, _dec(row.get("p")), _dec(row.get("v")),
                    str(row.get("S") or ""), _event_time(row, timestamp),
                )
        elif topic.startswith("orderbook.") and isinstance(rows[0], dict):
            self.features.ingest_orderbook(
                symbol, rows[0].get("b") or [], rows[0].get("a") or [], timestamp,
                snapshot=str(message.get("type") or "").lower() == "snapshot",
            )
        elif topic.startswith("allLiquidation."):
            for row in rows:
                self.features.ingest_liquidation(
                    symbol, str(row.get("S") or ""), _dec(row.get("p")),
                    _dec(row.get("v")), _event_time(row, timestamp),
                )


class BybitRestMetricsPoller:
    """Bounded funding/OI/ticker fallback with per-symbol failure isolation."""

    def __init__(
        self, settings: Settings, features: RollingFeatureEngine,
        http_get: Callable[[str, dict[str, str], float], dict[str, Any]] | None = None,
    ) -> None:
        self.settings = settings
        self.features = features
        self._http_get = http_get or _rest_get
        self.last_polled_at: datetime | None = None
        self.failures: dict[Symbol, str] = {}

    def poll(self, symbols: tuple[Symbol, ...]) -> None:
        now = datetime.now(timezone.utc)
        for symbol in symbols:
            try:
                scope = {"category": "linear", "symbol": symbol.value}
                ticker = _result_first(self._get("/v5/market/tickers", scope))
                funding_rows = (self._get("/v5/market/funding/history", {**scope, "limit": "1"}).get("result") or {}).get("list") or []
                oi_rows = (self._get("/v5/market/open-interest", {**scope, "intervalTime": "5min", "limit": "1"}).get("result") or {}).get("list") or []
                funding = _dec(funding_rows[0].get("fundingRate"), "0") if funding_rows else None
                oi = _dec(oi_rows[0].get("openInterest"), "0") if oi_rows else None
                self.features.ingest_rest_metrics(
                    symbol, funding_rate=funding, open_interest=oi,
                    volume_24h=_dec(ticker.get("volume24h"), "0"), timestamp=now,
                )
                # REST ticker is also a bounded WS fallback.
                if symbol not in self.features.tickers:
                    self.features.ingest_ticker(symbol, ticker, now)
                self.failures.pop(symbol, None)
            except Exception as exc:
                self.failures[symbol] = type(exc).__name__
                state = self.features.source_states["rest"]
                state.health = SourceHealth.DEGRADED
                state.last_error = type(exc).__name__
        self.last_polled_at = now

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        payload = self._http_get(
            f"{self.settings.v2_public_rest_url.rstrip('/')}{path}", params,
            self.settings.market_data_timeout_seconds,
        )
        if int(payload.get("retCode", -1)) != 0:
            raise ValueError("Bybit public REST metric request failed")
        return payload


def _dec(value: object, default: str | None = None) -> Decimal:
    if value in (None, ""):
        if default is None:
            raise ValueError("required market decimal missing")
        return Decimal(default)
    return Decimal(str(value))


def _event_time(row: dict[str, Any], fallback: datetime) -> datetime:
    value = row.get("T") or row.get("time")
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc) if value else fallback


def _momentum(points: list[TradePoint], last: Decimal) -> Decimal:
    return (last / points[0].price - 1) * Decimal("10000") if points else Decimal("0")


def _breakout_distance(points: list[TradePoint], last: Decimal) -> Decimal:
    if len(points) < 2:
        return Decimal("0")
    prior = points[:-1]
    high = max(p.price for p in prior); low = min(p.price for p in prior)
    if last > high:
        return (last / high - 1) * Decimal("10000")
    if last < low:
        return (last / low - 1) * Decimal("10000")
    return Decimal("0")


def _volume_acceleration(points: list[TradePoint], now: datetime, seconds: int) -> Decimal:
    midpoint = now - timedelta(seconds=seconds / 2)
    recent = sum((p.quantity for p in points if p.timestamp >= midpoint), Decimal("0"))
    old = sum((p.quantity for p in points if p.timestamp < midpoint), Decimal("0"))
    return recent / old if old > 0 else (Decimal("1") if recent > 0 else Decimal("0"))


def _trade_imbalance(points: list[TradePoint]) -> Decimal:
    buy = sum((p.quantity for p in points if p.side == "BUY"), Decimal("0"))
    sell = sum((p.quantity for p in points if p.side == "SELL"), Decimal("0"))
    total = buy + sell
    return (buy - sell) / total if total else Decimal("0")


def _volatility(points: list[TradePoint]) -> Decimal:
    if len(points) < 3:
        return Decimal("0")
    returns = [float(points[i].price / points[i - 1].price - 1) for i in range(1, len(points))]
    return Decimal(str(pstdev(returns) * 10000))


def _atr_bps(prices: list[Decimal], last: Decimal) -> Decimal:
    if len(prices) < 2:
        return Decimal("0")
    moves = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    return sum(moves, Decimal("0")) / Decimal(len(moves)) / last * Decimal("10000")


def _series_change(
    points: deque[tuple[datetime, Decimal]], now: datetime, window: timedelta,
) -> Decimal | None:
    rows = [item for item in points if now - item[0] <= window]
    if len(rows) < 2 or rows[0][1] == 0:
        return None
    return (rows[-1][1] / rows[0][1] - 1) * Decimal("100")


def _regime(momentum_bps: Decimal, volatility_bps: Decimal) -> str:
    if volatility_bps > Decimal("30"):
        return "HIGH_VOLATILITY"
    if momentum_bps > Decimal("20"):
        return "TRENDING_UP"
    if momentum_bps < Decimal("-20"):
        return "TRENDING_DOWN"
    return "RANGE"


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, int((len(values) - 1) * percentile)))
    return values[index]


def _rest_get(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "ByBot/2"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _result_first(payload: dict[str, Any]) -> dict[str, Any]:
    rows = (payload.get("result") or {}).get("list") or []
    if not rows:
        raise ValueError("public REST response has no rows")
    return rows[0]


def _apply_book_updates(
    levels: dict[Decimal, Decimal], rows: list[list[object]]
) -> None:
    for row in rows:
        if len(row) < 2:
            continue
        price = _dec(row[0]); quantity = _dec(row[1])
        if price <= 0:
            continue
        if quantity <= 0:
            levels.pop(price, None)
        else:
            levels[price] = quantity
