from __future__ import annotations

import asyncio
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from statistics import pstdev
from threading import RLock
import time
from typing import Any, Callable, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import websockets

from app.microstructure.calculations import build_leg_snapshot
from app.microstructure.models import LegSnapshot


PublicHttpGet = Callable[[str, dict[str, str], float], dict[str, Any]]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _public_get(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
    query = urlencode(sorted(params.items()))
    request = Request(
        f"{url}?{query}" if query else url,
        headers={"User-Agent": "ByBot-Microstructure-Shadow/1.0 READ_ONLY"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Bybit public response must be an object")
    return payload


class BybitPublicReadOnlyClient:
    """Allowlisted public GET client with no private or order endpoints."""

    ALLOWED_PATHS = {
        "/v5/market/time",
        "/v5/market/tickers",
        "/v5/market/instruments-info",
        "/v5/market/orderbook",
        "/v5/market/funding/history",
        "/v5/market/open-interest",
    }

    def __init__(
        self,
        base_url: str = "https://api.bybit.com",
        *,
        timeout_seconds: float = 5,
        http_get: PublicHttpGet | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get or _public_get
        self.exchange_mutation_capable = False
        self.request_count = 0

    def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if path not in self.ALLOWED_PATHS:
            raise ValueError("public microstructure client path is not allowlisted")
        payload = self.http_get(
            f"{self.base_url}{path}", params or {}, self.timeout_seconds
        )
        self.request_count += 1
        if int(payload.get("retCode", -1)) != 0:
            raise ValueError(
                f"Bybit public read failed: path={path} retCode={payload.get('retCode')}"
            )
        return payload

    def tickers(self, category: str) -> tuple[list[dict[str, Any]], datetime]:
        payload = self.get("/v5/market/tickers", {"category": category})
        return list((payload.get("result") or {}).get("list") or []), _payload_time(payload)

    def instrument(self, category: str, symbol: str) -> dict[str, Any]:
        payload = self.get(
            "/v5/market/instruments-info",
            {"category": category, "symbol": symbol},
        )
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            raise ValueError(f"instrument metadata missing: {category}:{symbol}")
        return dict(rows[0])

    def orderbook(self, category: str, symbol: str) -> dict[str, Any]:
        payload = self.get(
            "/v5/market/orderbook",
            {"category": category, "symbol": symbol, "limit": "50"},
        )
        return dict(payload.get("result") or {})

    def funding_history(self, symbol: str, *, limit: int = 10) -> list[dict[str, Any]]:
        payload = self.get(
            "/v5/market/funding/history",
            {"category": "linear", "symbol": symbol, "limit": str(limit)},
        )
        return list((payload.get("result") or {}).get("list") or [])

    def open_interest(self, symbol: str) -> tuple[Decimal | None, datetime | None]:
        payload = self.get(
            "/v5/market/open-interest",
            {
                "category": "linear", "symbol": symbol,
                "intervalTime": "5min", "limit": "1",
            },
        )
        rows = (payload.get("result") or {}).get("list") or []
        if not rows:
            return None, None
        value = _optional_decimal(rows[0].get("openInterest"))
        timestamp = _millisecond_time(rows[0].get("timestamp"))
        return value, timestamp

    def clock_offset_ms(self) -> tuple[Decimal, Decimal]:
        started = utc_now()
        payload = self.get("/v5/market/time")
        finished = utc_now()
        server = _payload_time(payload)
        midpoint = started + (finished - started) / 2
        offset = Decimal(str((server - midpoint).total_seconds() * 1000))
        round_trip = Decimal(str((finished - started).total_seconds() * 1000))
        return offset, round_trip


@dataclass
class _LegState:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    book_timestamp: datetime | None = None
    received_at: datetime | None = None
    update_id: int | None = None
    sequence: int | None = None
    ticker: dict[str, Any] = field(default_factory=dict)
    ticker_timestamp: datetime | None = None
    funding_timestamp: datetime | None = None
    trades: deque[tuple[datetime, Decimal, Decimal]] = field(
        default_factory=lambda: deque(maxlen=50_000)
    )
    midpoints: deque[tuple[datetime, Decimal]] = field(
        default_factory=lambda: deque(maxlen=50_000)
    )
    open_interest: deque[tuple[datetime, Decimal]] = field(
        default_factory=lambda: deque(maxlen=2_000)
    )
    funding_interval_minutes: int | None = None


class MicrostructureMarketState:
    def __init__(self) -> None:
        self._lock = RLock()
        self._states: dict[tuple[str, str], _LegState] = defaultdict(_LegState)
        self.reconnects: Counter[str] = Counter()
        self.last_message_at: dict[str, datetime] = {}
        self.last_error: dict[str, str] = {}

    def ingest_ticker(
        self,
        category: str,
        symbol: str,
        data: dict[str, Any],
        exchange_timestamp: datetime,
        received_at: datetime,
    ) -> None:
        with self._lock:
            state = self._states[(category, symbol)]
            state.ticker.update(data)
            state.ticker_timestamp = exchange_timestamp
            if category == "linear" and data.get("fundingRate") not in (None, ""):
                state.funding_timestamp = exchange_timestamp
            self.last_message_at[category] = received_at

    def ingest_trade(
        self,
        category: str,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        exchange_timestamp: datetime,
        received_at: datetime,
    ) -> None:
        if price <= 0 or quantity < 0:
            return
        with self._lock:
            state = self._states[(category, symbol)]
            state.trades.append((exchange_timestamp, price, quantity))
            self.last_message_at[category] = received_at

    def ingest_orderbook(
        self,
        category: str,
        symbol: str,
        bids: Sequence[Sequence[object]],
        asks: Sequence[Sequence[object]],
        exchange_timestamp: datetime,
        received_at: datetime,
        *,
        snapshot: bool,
        update_id: int | None = None,
        sequence: int | None = None,
    ) -> None:
        with self._lock:
            state = self._states[(category, symbol)]
            if update_id == 1:
                snapshot = True
            if not snapshot:
                if update_id is not None and state.update_id is not None and update_id <= state.update_id:
                    return
                if sequence is not None and state.sequence is not None and sequence <= state.sequence:
                    return
            if snapshot:
                state.bids.clear()
                state.asks.clear()
            _apply_levels(state.bids, bids)
            _apply_levels(state.asks, asks)
            if not state.bids or not state.asks:
                return
            best_bid = max(state.bids)
            best_ask = min(state.asks)
            if best_bid >= best_ask:
                return
            state.book_timestamp = exchange_timestamp
            state.received_at = received_at
            state.update_id = update_id
            state.sequence = sequence
            state.midpoints.append((exchange_timestamp, (best_bid + best_ask) / Decimal("2")))
            self.last_message_at[category] = received_at

    def ingest_open_interest(
        self, symbol: str, value: Decimal, timestamp: datetime,
    ) -> None:
        with self._lock:
            self._states[("linear", symbol)].open_interest.append((timestamp, value))

    def set_funding_interval(self, symbol: str, minutes: int | None) -> None:
        with self._lock:
            self._states[("linear", symbol)].funding_interval_minutes = minutes

    def snapshot(self, category: str, symbol: str) -> LegSnapshot | None:
        with self._lock:
            state = self._states.get((category, symbol))
            if state is None or not state.bids or not state.asks:
                return None
            book_timestamp = state.book_timestamp
            received_at = state.received_at
            if book_timestamp is None or received_at is None:
                return None
            bids = sorted(state.bids.items(), reverse=True)[:50]
            asks = sorted(state.asks.items())[:50]
            ticker = dict(state.ticker)
            recent_trade = state.trades[-1] if state.trades else None
            oi_rows = tuple(state.open_interest)
            oi = oi_rows[-1] if oi_rows else None
            oi_change = None
            if len(oi_rows) >= 2 and oi_rows[-2][1] > 0:
                oi_change = (
                    oi_rows[-1][1] / oi_rows[-2][1] - Decimal("1")
                ) * Decimal("100")
            volatility = _volatility_bps(tuple(state.trades), book_timestamp)
            return build_leg_snapshot(
                category=category,
                symbol=symbol,
                exchange_timestamp=book_timestamp,
                local_receive_timestamp=received_at,
                bids=bids,
                asks=asks,
                recent_trade_price=recent_trade[1] if recent_trade else None,
                recent_trade_volume=recent_trade[2] if recent_trade else None,
                recent_trade_timestamp=recent_trade[0] if recent_trade else None,
                ticker=ticker,
                funding_timestamp=state.funding_timestamp,
                funding_interval_minutes=state.funding_interval_minutes,
                open_interest=oi[1] if oi else None,
                open_interest_timestamp=oi[0] if oi else None,
                open_interest_change_pct=oi_change,
                volatility_5m_bps=volatility,
            )

    def quote_paths(
        self, category: str, symbol: str,
    ) -> tuple[list[tuple[datetime, Decimal]], list[tuple[datetime, Decimal]]]:
        with self._lock:
            state = self._states.get((category, symbol))
            if state is None:
                return [], []
            return (
                [(timestamp, price) for timestamp, price, _ in state.trades],
                list(state.midpoints),
            )

    def book_age_seconds(self, category: str, symbol: str, now: datetime) -> float | None:
        with self._lock:
            state = self._states.get((category, symbol))
            if state is None or state.book_timestamp is None:
                return None
            return max(0.0, (now - state.book_timestamp).total_seconds())


class PublicWebSocketPump:
    """Public ticker/trade/book pump; reconnects without any execution hook."""

    def __init__(
        self,
        *,
        category: str,
        url: str,
        state: MicrostructureMarketState,
        max_reconnect_seconds: int = 30,
    ) -> None:
        self.category = category
        self.url = url
        self.state = state
        self.max_reconnect_seconds = max_reconnect_seconds
        self.running = False

    async def run(self, symbols: Sequence[str]) -> None:
        self.running = True
        delay = 1
        while self.running:
            try:
                async with websockets.connect(
                    self.url, open_timeout=10, ping_interval=20, ping_timeout=20,
                ) as socket:
                    topics = [
                        topic for symbol in symbols for topic in (
                            f"tickers.{symbol}",
                            f"publicTrade.{symbol}",
                            f"orderbook.50.{symbol}",
                        )
                    ]
                    for index in range(0, len(topics), 10):
                        await socket.send(json.dumps({
                            "op": "subscribe", "args": topics[index:index + 10],
                        }))
                    delay = 1
                    async for raw in socket:
                        self.handle_message(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state.reconnects[self.category] += 1
                self.state.last_error[self.category] = type(exc).__name__
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_reconnect_seconds)

    def stop(self) -> None:
        self.running = False

    def handle_message(self, message: dict[str, Any]) -> None:
        topic = str(message.get("topic") or "")
        data = message.get("data")
        if not topic or data is None:
            return
        received_at = utc_now()
        fallback = _millisecond_time(message.get("ts")) or received_at
        symbol = topic.rsplit(".", 1)[-1]
        rows = data if isinstance(data, list) else [data]
        if topic.startswith("tickers.") and rows and isinstance(rows[0], dict):
            self.state.ingest_ticker(
                self.category, symbol, rows[0], fallback, received_at
            )
        elif topic.startswith("publicTrade."):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                timestamp = _millisecond_time(row.get("T")) or fallback
                price = _optional_decimal(row.get("p"))
                quantity = _optional_decimal(row.get("v"))
                if price is not None and quantity is not None:
                    self.state.ingest_trade(
                        self.category, symbol, price, quantity, timestamp, received_at
                    )
        elif topic.startswith("orderbook.") and rows and isinstance(rows[0], dict):
            row = rows[0]
            timestamp = _millisecond_time(row.get("cts")) or fallback
            self.state.ingest_orderbook(
                self.category, symbol, row.get("b") or [], row.get("a") or [],
                timestamp, received_at,
                snapshot=str(message.get("type") or "").lower() == "snapshot",
                update_id=int(row["u"]) if row.get("u") is not None else None,
                sequence=int(row["seq"]) if row.get("seq") is not None else None,
            )


def select_liquid_spot_perp_universe(
    client: BybitPublicReadOnlyClient,
    *,
    candidates: Sequence[str],
    size: int,
    minimum_leg_turnover_usdt: Decimal,
    maximum_spread_bps: Decimal,
) -> list[dict[str, Any]]:
    spot_rows, observed_at = client.tickers("spot")
    linear_rows, _ = client.tickers("linear")
    spot = {str(row.get("symbol")): row for row in spot_rows}
    linear = {str(row.get("symbol")): row for row in linear_rows}
    decisions: list[dict[str, Any]] = []
    eligible: list[tuple[Decimal, str, dict[str, Any]]] = []
    for symbol in sorted(set(candidates)):
        spot_row = spot.get(symbol)
        perp_row = linear.get(symbol)
        reasons: list[str] = []
        if spot_row is None:
            reasons.append("SPOT_MARKET_MISSING")
        if perp_row is None:
            reasons.append("LINEAR_PERPETUAL_MISSING")
        spot_turnover = _optional_decimal((spot_row or {}).get("turnover24h"))
        perp_turnover = _optional_decimal((perp_row or {}).get("turnover24h"))
        spot_spread = _ticker_spread(spot_row)
        perp_spread = _ticker_spread(perp_row)
        score = (
            min(spot_turnover, perp_turnover)
            if spot_turnover is not None and perp_turnover is not None else ZERO
        )
        if score < minimum_leg_turnover_usdt:
            reasons.append("MINIMUM_TWO_LEG_TURNOVER_NOT_MET")
        if spot_spread is None or perp_spread is None:
            reasons.append("TOP_OF_BOOK_MISSING")
        elif max(spot_spread, perp_spread) > maximum_spread_bps:
            reasons.append("TWO_LEG_SPREAD_TOO_WIDE")
        decision = {
            "symbol": symbol,
            "selected": False,
            "selection_score_min_leg_turnover_usdt": str(score),
            "spot_turnover_24h_usdt": (
                str(spot_turnover) if spot_turnover is not None else None
            ),
            "perp_turnover_24h_usdt": (
                str(perp_turnover) if perp_turnover is not None else None
            ),
            "spot_spread_bps": str(spot_spread) if spot_spread is not None else None,
            "perp_spread_bps": str(perp_spread) if perp_spread is not None else None,
            "reasons": reasons,
            "observed_at": observed_at.isoformat(),
        }
        decisions.append(decision)
        if not reasons:
            eligible.append((score, symbol, decision))
    selected = sorted(eligible, key=lambda row: (-row[0], row[1]))[:size]
    selected_symbols = {symbol for _, symbol, _ in selected}
    for decision in decisions:
        if decision["symbol"] in selected_symbols:
            decision["selected"] = True
            decision["reasons"] = ["TOP_TWO_LEG_LIQUIDITY_RANK"]
        elif not decision["reasons"]:
            decision["reasons"] = ["ELIGIBLE_NOT_IN_INITIAL_LIQUIDITY_TOP_N"]
    if not selected_symbols:
        raise RuntimeError("no liquid spot/perpetual symbol passed selection")
    return decisions


def bootstrap_market_state(
    client: BybitPublicReadOnlyClient,
    state: MicrostructureMarketState,
    symbols: Sequence[str],
) -> dict[str, Any]:
    now = utc_now()
    spot_tickers, spot_at = client.tickers("spot")
    perp_tickers, perp_at = client.tickers("linear")
    ticker_maps = {
        "spot": {str(row.get("symbol")): row for row in spot_tickers},
        "linear": {str(row.get("symbol")): row for row in perp_tickers},
    }
    failures: dict[str, str] = {}
    for symbol in symbols:
        try:
            linear_instrument = client.instrument("linear", symbol)
            interval = int(linear_instrument.get("fundingInterval") or 0) or None
            state.set_funding_interval(symbol, interval)
            for category, ticker_at in (("spot", spot_at), ("linear", perp_at)):
                ticker = ticker_maps[category].get(symbol)
                if ticker:
                    state.ingest_ticker(category, symbol, ticker, ticker_at, now)
                book = client.orderbook(category, symbol)
                timestamp = (
                    _millisecond_time(book.get("cts") or book.get("ts")) or now
                )
                state.ingest_orderbook(
                    category, symbol, book.get("b") or [], book.get("a") or [],
                    timestamp, now, snapshot=True,
                    update_id=int(book["u"]) if book.get("u") is not None else None,
                    sequence=int(book["seq"]) if book.get("seq") is not None else None,
                )
            oi, oi_at = client.open_interest(symbol)
            if oi is not None and oi_at is not None:
                state.ingest_open_interest(symbol, oi, oi_at)
        except Exception as exc:
            failures[symbol] = type(exc).__name__
    return {"failures": failures, "completed_at": utc_now().isoformat()}


def refresh_market_state(
    client: BybitPublicReadOnlyClient,
    state: MicrostructureMarketState,
    symbols: Sequence[str],
    *,
    stale_book_seconds: float = 15,
    stage_timings_ms: dict[str, float] | None = None,
) -> dict[str, str]:
    now = utc_now()
    failures: dict[str, str] = {}
    rest_ms = 0.0
    oi_ms = 0.0
    try:
        started = time.perf_counter()
        spot_rows, spot_at = client.tickers("spot")
        linear_rows, linear_at = client.tickers("linear")
        rest_ms += (time.perf_counter() - started) * 1000
        maps = {
            "spot": {str(row.get("symbol")): row for row in spot_rows},
            "linear": {str(row.get("symbol")): row for row in linear_rows},
        }
        for symbol in symbols:
            for category, timestamp in (("spot", spot_at), ("linear", linear_at)):
                row = maps[category].get(symbol)
                if row:
                    state.ingest_ticker(category, symbol, row, timestamp, now)
                age = state.book_age_seconds(category, symbol, now)
                if age is None or age > stale_book_seconds:
                    started = time.perf_counter()
                    book = client.orderbook(category, symbol)
                    rest_ms += (time.perf_counter() - started) * 1000
                    book_at = _millisecond_time(book.get("cts") or book.get("ts")) or now
                    state.ingest_orderbook(
                        category, symbol, book.get("b") or [], book.get("a") or [],
                        book_at, now, snapshot=True,
                        update_id=int(book["u"]) if book.get("u") is not None else None,
                        sequence=int(book["seq"]) if book.get("seq") is not None else None,
                    )
        oi_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=max(1, min(5, len(symbols)))) as executor:
            futures = {
                executor.submit(client.open_interest, symbol): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    oi, oi_at = future.result()
                    if oi is not None and oi_at is not None:
                        state.ingest_open_interest(symbol, oi, oi_at)
                except Exception as exc:
                    failures[f"oi:{symbol}"] = type(exc).__name__
        oi_ms += (time.perf_counter() - oi_started) * 1000
    except Exception as exc:
        failures["global"] = type(exc).__name__
    if stage_timings_ms is not None:
        stage_timings_ms["required_rest_refresh_ms"] = rest_ms
        stage_timings_ms["oi_refresh_ms"] = oi_ms
    return failures


def _apply_levels(
    target: dict[Decimal, Decimal], rows: Sequence[Sequence[object]],
) -> None:
    for row in rows:
        if len(row) < 2:
            continue
        price = Decimal(str(row[0]))
        quantity = Decimal(str(row[1]))
        if price <= 0 or quantity < 0:
            continue
        if quantity == 0:
            target.pop(price, None)
        else:
            target[price] = quantity


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ArithmeticError, ValueError):
        return None


def _millisecond_time(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _payload_time(payload: dict[str, Any]) -> datetime:
    timestamp = _millisecond_time(payload.get("time"))
    if timestamp is not None:
        return timestamp
    result = payload.get("result") or {}
    seconds = result.get("timeSecond")
    if seconds not in (None, ""):
        return datetime.fromtimestamp(int(seconds), tz=timezone.utc)
    return utc_now()


def _ticker_spread(row: dict[str, Any] | None) -> Decimal | None:
    if row is None:
        return None
    bid = _optional_decimal(row.get("bid1Price"))
    ask = _optional_decimal(row.get("ask1Price"))
    if bid is None or ask is None or bid <= 0 or ask <= bid:
        return None
    return (ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("10000")


def _volatility_bps(
    trades: Sequence[tuple[datetime, Decimal, Decimal]], now: datetime,
) -> Decimal | None:
    prices = [
        price for timestamp, price, _ in trades
        if now - timestamp <= timedelta(minutes=5)
    ]
    if len(prices) < 3:
        return None
    returns = [float(prices[index] / prices[index - 1] - Decimal("1")) for index in range(1, len(prices))]
    return Decimal(str(pstdev(returns) * 10000))


ZERO = Decimal("0")
