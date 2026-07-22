from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from statistics import pstdev
from threading import RLock
from typing import Any, Awaitable, Callable, Sequence
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


@dataclass(frozen=True)
class OrderFlowPoint:
    timestamp: datetime
    normalized_imbalance: Decimal


@dataclass(frozen=True)
class _FeatureStateSnapshot:
    """Immutable, internally consistent view captured under the engine lock."""

    ticker: dict[str, Any]
    book: tuple[Decimal, Decimal, Decimal, Decimal, datetime]
    book_levels: tuple[dict[Decimal, Decimal], dict[Decimal, Decimal]]
    executable_depth: tuple[Decimal, Decimal]
    trades: tuple[TradePoint, ...]
    btc_trades: tuple[TradePoint, ...]
    liquidations: tuple[LiquidationPoint, ...]
    order_flow: tuple[OrderFlowPoint, ...]
    funding: tuple[Decimal, datetime] | None
    open_interest: tuple[tuple[datetime, Decimal], ...]
    source_states: dict[str, SourceState]
    liquidation_invalid: bool
    liquidation_subscribed: bool
    liquidation_unsupported: bool


class RollingFeatureEngine:
    """Deterministic per-symbol numeric windows fed by WS or tests."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Writers and the immutable reader capture use this one lock.  The
        # expensive feature calculations intentionally run after it is released.
        self._state_lock = RLock()
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
        self._book_update_ids: dict[Symbol, int] = {}
        self._book_sequences: dict[Symbol, int] = {}
        self.executable_depth: dict[Symbol, tuple[Decimal, Decimal]] = {}
        self.order_flow: dict[Symbol, deque[OrderFlowPoint]] = defaultdict(
            lambda: deque(maxlen=limit)
        )
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
        self.liquidation_subscriptions: dict[Symbol, datetime] = {}
        self.unsupported_liquidation_symbols: set[Symbol] = set()
        self._source_age_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=10_000)
        )

    def ingest_ticker(self, symbol: Symbol, data: dict[str, Any], timestamp: datetime) -> None:
        # Bybit ticker messages after the initial snapshot are sparse deltas.
        # Merge them so a funding-only delta cannot erase lastPrice.
        with self._state_lock:
            ticker = self.tickers.setdefault(symbol, {})
            ticker.update(data)
            ticker["timestamp"] = timestamp
            self._healthy("ticker", timestamp)

    def ingest_trade(
        self, symbol: Symbol, price: Decimal, quantity: Decimal,
        side: str, timestamp: datetime,
    ) -> None:
        with self._state_lock:
            self.trades[symbol].append(TradePoint(timestamp, price, quantity, side.upper()))
            self._healthy("trades", timestamp)

    def ingest_orderbook(
        self, symbol: Symbol, bids: list[list[object]], asks: list[list[object]],
        timestamp: datetime, *, snapshot: bool = True,
        update_id: int | None = None, sequence: int | None = None,
    ) -> None:
        with self._state_lock:
            self._ingest_orderbook_locked(
                symbol, bids, asks, timestamp, snapshot=snapshot,
                update_id=update_id, sequence=sequence,
            )

    def _ingest_orderbook_locked(
        self, symbol: Symbol, bids: list[list[object]], asks: list[list[object]],
        timestamp: datetime, *, snapshot: bool,
        update_id: int | None, sequence: int | None,
    ) -> None:
        if update_id == 1:
            snapshot = True
        if not snapshot:
            prior_update = self._book_update_ids.get(symbol)
            prior_sequence = self._book_sequences.get(symbol)
            if (
                (update_id is not None and prior_update is not None and update_id <= prior_update)
                or (sequence is not None and prior_sequence is not None and sequence <= prior_sequence)
            ):
                state = self.source_states["orderbook"]
                state.health = SourceHealth.DEGRADED
                state.last_error = "out-of-order orderbook delta rejected"
                return
        previous_best: tuple[Decimal, Decimal, Decimal, Decimal] | None = None
        if symbol in self._book_levels:
            old_bids, old_asks = self._book_levels[symbol]
            if old_bids and old_asks:
                old_bid = max(old_bids); old_ask = min(old_asks)
                previous_best = (
                    old_bid, old_bids[old_bid], old_ask, old_asks[old_ask]
                )
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
        bid_floor = bid * (Decimal("1") - Decimal("0.001"))
        ask_ceiling = ask * (Decimal("1") + Decimal("0.001"))
        bid_depth_10bps = sum(
            (price * quantity for price, quantity in bid_levels.items() if price >= bid_floor),
            Decimal("0"),
        )
        ask_depth_10bps = sum(
            (price * quantity for price, quantity in ask_levels.items() if price <= ask_ceiling),
            Decimal("0"),
        )
        self.books[symbol] = (bid, ask, bid_depth, ask_depth, timestamp)
        if previous_best is not None and not snapshot:
            old_bid, old_bid_qty, old_ask, old_ask_qty = previous_best
            bid_qty = bid_levels[bid]; ask_qty = ask_levels[ask]
            event = Decimal("0")
            if bid >= old_bid:
                event += bid_qty
            if bid <= old_bid:
                event -= old_bid_qty
            if ask <= old_ask:
                event -= ask_qty
            if ask >= old_ask:
                event += old_ask_qty
            denominator = max(old_bid_qty + old_ask_qty, Decimal("0.00000001"))
            self.order_flow[symbol].append(
                OrderFlowPoint(timestamp, max(Decimal("-5"), min(Decimal("5"), event / denominator)))
            )
        self.executable_depth[symbol] = (bid_depth_10bps, ask_depth_10bps)
        if update_id is not None:
            self._book_update_ids[symbol] = update_id
        if sequence is not None:
            self._book_sequences[symbol] = sequence
        self._healthy("orderbook", timestamp)

    def ingest_liquidation(
        self, symbol: Symbol, side: str, price: Decimal,
        quantity: Decimal, timestamp: datetime,
    ) -> None:
        with self._state_lock:
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
            state = self.source_states["liquidations"]
            state.last_event_at = timestamp
            self._healthy("liquidations", timestamp)

    def mark_transport_connected(self, connected: bool, *, at: datetime | None = None) -> None:
        current = at or datetime.now(timezone.utc)
        with self._state_lock:
            for state in self.source_states.values():
                state.connected = connected
                state.last_heartbeat_at = current
                if not connected:
                    state.health = SourceHealth.DEGRADED

    def mark_liquidation_subscribed(
        self, symbols: tuple[Symbol, ...], *, at: datetime | None = None,
        unsupported: tuple[Symbol, ...] = (),
    ) -> None:
        current = at or datetime.now(timezone.utc)
        with self._state_lock:
            state = self.source_states["liquidations"]
            state.connected = True
            state.subscribed = True
            state.subscription_confirmed_at = current
            state.last_heartbeat_at = current
            state.health = SourceHealth.OK
            state.last_error = None
            self.unsupported_liquidation_symbols.update(unsupported)
            for symbol in symbols:
                if symbol not in self.unsupported_liquidation_symbols:
                    self.liquidation_subscriptions[symbol] = current

    def ingest_rest_metrics(
        self, symbol: Symbol, *, funding_rate: Decimal | None,
        open_interest: Decimal | None, volume_24h: Decimal | None,
        timestamp: datetime,
    ) -> None:
        with self._state_lock:
            if funding_rate is not None:
                self.funding[symbol] = (funding_rate, timestamp)
            if open_interest is not None:
                self.open_interest[symbol].append((timestamp, open_interest))
            ticker = self.tickers.setdefault(symbol, {})
            if volume_24h is not None:
                ticker["volume24h"] = volume_24h
            self._healthy("rest", timestamp)

    def has_ticker(self, symbol: Symbol) -> bool:
        with self._state_lock:
            return symbol in self.tickers

    def mark_source_degraded(
        self, source: str, error: str, *, increment_reconnect: bool = False,
        subscribed: bool | None = None,
    ) -> None:
        with self._state_lock:
            state = self.source_states[source]
            state.health = SourceHealth.DEGRADED
            state.last_error = error
            if subscribed is not None:
                state.subscribed = subscribed
            if increment_reconnect:
                state.reconnects += 1

    def _capture_state(self, symbol: Symbol) -> _FeatureStateSnapshot | None:
        with self._state_lock:
            ticker = self.tickers.get(symbol)
            book = self.books.get(symbol)
            levels = self._book_levels.get(symbol)
            if not ticker or not book or levels is None:
                return None
            bid_levels, ask_levels = levels
            return _FeatureStateSnapshot(
                ticker=dict(ticker), book=book,
                book_levels=(dict(bid_levels), dict(ask_levels)),
                executable_depth=self.executable_depth.get(
                    symbol, (book[2], book[3])
                ),
                trades=tuple(self.trades.get(symbol, ())),
                btc_trades=tuple(self.trades.get(Symbol.BTCUSDT, ())),
                liquidations=tuple(self.liquidations.get(symbol, ())),
                order_flow=tuple(self.order_flow.get(symbol, ())),
                funding=self.funding.get(symbol),
                open_interest=tuple(self.open_interest.get(symbol, ())),
                source_states={
                    key: value.model_copy(deep=True)
                    for key, value in self.source_states.items()
                },
                liquidation_invalid=symbol in self.invalid_liquidation_symbols,
                liquidation_subscribed=symbol in self.liquidation_subscriptions,
                liquidation_unsupported=symbol in self.unsupported_liquidation_symbols,
            )

    def snapshot(
        self, symbol: Symbol, *, now: datetime | None = None,
        btc_snapshot: MarketFeatureSnapshot | None = None,
    ) -> MarketFeatureSnapshot | None:
        current = now or datetime.now(timezone.utc)
        state_snapshot = self._capture_state(symbol)
        if state_snapshot is None:
            return None
        ticker = state_snapshot.ticker
        book = state_snapshot.book
        trades = state_snapshot.trades
        liquidations = state_snapshot.liquidations
        order_flow_points = state_snapshot.order_flow
        open_interest = state_snapshot.open_interest
        last = _dec(ticker.get("lastPrice") or ticker.get("price"))
        bid, ask, bid_depth, ask_depth, book_time = book
        ticker_time = ticker.get("timestamp")
        trade_time = trades[-1].timestamp if trades else None
        liquidation_time = (
            liquidations[-1].timestamp if liquidations else None
        )
        source_timestamps = {
            "ticker": ticker_time if isinstance(ticker_time, datetime) else None,
            "orderbook": book_time,
            "trades": trade_time,
            "liquidations": liquidation_time,
            "funding": state_snapshot.funding[1] if state_snapshot.funding else None,
            "open_interest": (
                open_interest[-1][0] if open_interest else None
            ),
        }
        source_ages = {
            source: max(0.0, (current - timestamp).total_seconds())
            if timestamp is not None else None
            for source, timestamp in source_timestamps.items()
        }
        for source, age in source_ages.items():
            if age is not None:
                with self._state_lock:
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
        if not trades:
            stale_reasons.append("public trades are unavailable")
        elif (current - trades[-1].timestamp).total_seconds() > (
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
            with self._state_lock:
                self.stale_feature_observations += 1
        momentum: dict[str, Decimal] = {}
        breakout: dict[str, Decimal] = {}
        acceleration: dict[str, Decimal] = {}
        imbalance: dict[str, Decimal] = {}
        volatility: dict[str, Decimal] = {}
        order_flow: dict[str, Decimal] = {}
        observation_count: dict[str, int] = {}
        window_coverage: dict[str, Decimal] = {}
        for label, seconds in WINDOWS.items():
            window = [p for p in trades if current - p.timestamp <= timedelta(seconds=seconds)]
            momentum[label] = _momentum(window, last)
            breakout[label] = _breakout_distance(window, last)
            acceleration[label] = min(
                _volume_acceleration(window, current, seconds),
                self.settings.v2_max_volume_acceleration,
            )
            imbalance[label] = _trade_imbalance(window)
            volatility[label] = _volatility(window)
            ofi_rows = [
                item.normalized_imbalance for item in order_flow_points
                if current - item.timestamp <= timedelta(seconds=seconds)
            ]
            order_flow[label] = (
                sum(ofi_rows, Decimal("0")) / Decimal(len(ofi_rows))
                if ofi_rows else Decimal("0")
            )
            observation_count[label] = len(window)
            window_coverage[label] = (
                Decimal(str(max(0.0, (window[-1].timestamp - window[0].timestamp).total_seconds())))
                if len(window) >= 2 else Decimal("0")
            )
        book_total = bid_depth + ask_depth
        book_imbalance = (
            (bid_depth - ask_depth) / book_total if book_total > 0 else Decimal("0")
        )
        spread_bps = (ask - bid) / ((ask + bid) / 2) * Decimal("10000")
        top_bid_qty = state_snapshot.book_levels[0].get(bid, Decimal("0"))
        top_ask_qty = state_snapshot.book_levels[1].get(ask, Decimal("0"))
        top_total = top_bid_qty + top_ask_qty
        microprice = (
            (ask * top_bid_qty + bid * top_ask_qty) / top_total
            if top_total > 0 else (bid + ask) / Decimal("2")
        )
        prices = [point.price for point in trades if current - point.timestamp <= timedelta(minutes=15)]
        local_high = max(prices, default=last); local_low = min(prices, default=last)
        liqs = [point for point in liquidations if current - point.timestamp <= timedelta(minutes=5)]
        # Bybit `S` is the liquidated position side: Buy means a LONG was
        # liquidated; Sell means a SHORT was liquidated.
        long_liq = sum((p.notional for p in liqs if p.side == "BUY"), Decimal("0"))
        short_liq = sum((p.notional for p in liqs if p.side == "SELL"), Decimal("0"))
        liq_total = short_liq + long_liq
        liquidation_state = state_snapshot.source_states["liquidations"]
        subscribed = state_snapshot.liquidation_subscribed
        transport_available = (
            liquidation_state.connected
            and liquidation_state.health not in {
                SourceHealth.UNAVAILABLE, SourceHealth.DEGRADED, SourceHealth.STALE,
            }
            and not state_snapshot.liquidation_unsupported
        )
        oi_change = _series_change(open_interest, current, timedelta(minutes=5))
        funding_rate = state_snapshot.funding[0] if state_snapshot.funding else None
        funding_deviation = funding_rate * Decimal("10000") if funding_rate is not None else None
        relative = Decimal("0")
        if btc_snapshot and symbol != Symbol.BTCUSDT:
            relative = momentum["5m"] - btc_snapshot.price_momentum.get("5m", Decimal("0"))
        market_regime = _regime(momentum["15m"], volatility["15m"])
        bid_depth_10bps, ask_depth_10bps = state_snapshot.executable_depth
        correlation, beta = _btc_relationship(
            list(trades), list(state_snapshot.btc_trades),
            current,
        ) if symbol != Symbol.BTCUSDT else (Decimal("1"), Decimal("1"))
        return MarketFeatureSnapshot(
            symbol=symbol, timestamp=current, fresh=fresh, stale_reasons=stale_reasons,
            last_price=last, bid_price=bid, ask_price=ask, spread_bps=spread_bps,
            bid_depth_usdt=bid_depth, ask_depth_usdt=ask_depth,
            bid_depth_10bps_usdt=bid_depth_10bps,
            ask_depth_10bps_usdt=ask_depth_10bps,
            price_momentum=momentum, breakout_distance_bps=breakout,
            volume_acceleration=acceleration, trade_imbalance=imbalance,
            order_flow_imbalance=order_flow,
            orderbook_imbalance=book_imbalance,
            microprice=microprice,
            microprice_deviation_bps=(microprice / last - Decimal("1")) * Decimal("10000"),
            realized_volatility=volatility,
            observation_count=observation_count,
            window_coverage_seconds=window_coverage,
            atr_bps=_atr_bps([
                point for point in trades
                if current - point.timestamp <= timedelta(minutes=15)
            ], last),
            distance_from_high_bps=(last - local_high) / last * Decimal("10000"),
            distance_from_low_bps=(last - local_low) / last * Decimal("10000"),
            relative_strength_vs_btc_bps=relative,
            rolling_correlation_vs_btc=correlation,
            btc_beta=beta,
            funding_rate=funding_rate,
            funding_deviation_bps=funding_deviation,
            open_interest=open_interest[-1][1] if open_interest else None,
            open_interest_change_pct=oi_change,
            liquidation_long_usdt=long_liq, liquidation_short_usdt=short_liq,
            liquidation_imbalance=(short_liq - long_liq) / liq_total if liq_total else Decimal("0"),
            volume_24h=_dec(ticker.get("volume24h"), "0"), market_regime=market_regime,
            source_health={key: value.health for key, value in state_snapshot.source_states.items()},
            source_timestamps=source_timestamps,
            source_age_seconds=source_ages,
            stale_evidence=stale_evidence,
            liquidation_last_valid_at=liquidation_time,
            # Per-symbol liquidation age must derive from that symbol's last
            # valid event. Generic source-message age remains available in
            # source_age_seconds and must never stand in for symbol recency.
            liquidation_data_age_seconds=(
                max(0.0, (current - liquidation_time).total_seconds())
                if liquidation_time else None
            ),
            liquidation_data_valid=not state_snapshot.liquidation_invalid,
            # Subscription initialization is intentionally independent from
            # event recency: a healthy stream can legitimately emit zero
            # liquidation events for a symbol.
            liquidation_feed_initialized=subscribed,
            liquidation_feed_available=transport_available,
            liquidation_connection_state=("CONNECTED" if liquidation_state.connected else "DISCONNECTED"),
            liquidation_subscription_state=(
                "UNSUPPORTED" if state_snapshot.liquidation_unsupported
                else "SUBSCRIBED" if subscribed else "NOT_SUBSCRIBED"
            ),
            liquidation_event_count_5m=len(liqs),
            liquidation_notional_5m=liq_total,
        )

    def record_critical_stale_incident(self) -> None:
        with self._state_lock:
            self.stale_incidents += 1

    def data_age_metrics(self) -> dict[str, dict[str, float | str | bool | None]]:
        with self._state_lock:
            samples_by_source = {
                source: tuple(self._source_age_samples.get(source, ()))
                for source in ("ticker", "trades", "orderbook", "liquidations", "rest")
            }
            states = {
                source: self.source_states[source].model_copy(deep=True)
                for source in samples_by_source
            }
        result: dict[str, dict[str, float | str | bool | None]] = {}
        for source in ("ticker", "trades", "orderbook", "liquidations", "rest"):
            samples = sorted(samples_by_source[source])
            state = states[source]
            latest_age = (
                max(0.0, (datetime.now(timezone.utc) - state.last_message_at).total_seconds())
                if state.last_message_at else None
            )
            result[source] = {
                "maximum": max(samples) if samples else None,
                "p50": _percentile(samples, 0.50),
                "p95": _percentile(samples, 0.95),
                "latest_message_age": latest_age,
                "connected": state.connected,
                "subscribed": state.subscribed,
                "last_heartbeat_at": (
                    state.last_heartbeat_at.isoformat()
                    if state.last_heartbeat_at else None
                ),
                "last_event_at": (
                    state.last_event_at.isoformat() if state.last_event_at else None
                ),
            }
        return result

    def _healthy(self, source: str, timestamp: datetime) -> None:
        state = self.source_states[source]
        state.health = SourceHealth.OK
        state.connected = True
        state.last_heartbeat_at = timestamp
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
                    self.features.mark_transport_connected(True)
                    self._requested_symbols = symbols
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
                self.features.mark_transport_connected(False)
                for source in ("ticker", "trades", "orderbook", "liquidations", "rest"):
                    self.features.mark_source_degraded(
                        source, type(exc).__name__, increment_reconnect=True
                    )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.settings.v2_ws_reconnect_max_seconds)

    def stop(self) -> None:
        self.running = False

    def handle_message(self, message: dict[str, Any]) -> None:
        if str(message.get("op") or "") == "subscribe":
            if bool(message.get("success")):
                raw_failed = (
                    message.get("failTopics")
                    or message.get("fail_topics")
                    or (
                        (message.get("data") or {}).get("failTopics")
                        if isinstance(message.get("data"), dict) else None
                    )
                    or []
                )
                unsupported: list[Symbol] = []
                for topic in raw_failed:
                    if not str(topic).startswith("allLiquidation."):
                        continue
                    try:
                        unsupported.append(Symbol(str(topic).rsplit(".", 1)[-1]))
                    except ValueError:
                        continue
                self.features.mark_liquidation_subscribed(
                    getattr(self, "_requested_symbols", ()),
                    unsupported=tuple(unsupported),
                )
            else:
                self.features.mark_source_degraded(
                    "liquidations", "liquidation subscription rejected",
                    subscribed=False,
                )
            return
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
                update_id=(int(rows[0]["u"]) if rows[0].get("u") is not None else None),
                sequence=(int(rows[0]["seq"]) if rows[0].get("seq") is not None else None),
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
                if not self.features.has_ticker(symbol):
                    self.features.ingest_ticker(symbol, ticker, now)
                self.failures.pop(symbol, None)
            except Exception as exc:
                self.failures[symbol] = type(exc).__name__
                self.features.mark_source_degraded("rest", type(exc).__name__)
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
    if old <= 0:
        return Decimal("0")
    return recent / old


def _trade_imbalance(points: list[TradePoint]) -> Decimal:
    buy = sum((p.quantity for p in points if p.side == "BUY"), Decimal("0"))
    sell = sum((p.quantity for p in points if p.side == "SELL"), Decimal("0"))
    total = buy + sell
    return (buy - sell) / total if total else Decimal("0")


def _volatility(points: list[TradePoint]) -> Decimal:
    prices = _time_bar_closes(points, seconds=1)
    if len(prices) < 3:
        return Decimal("0")
    returns = [float(prices[i] / prices[i - 1] - 1) for i in range(1, len(prices))]
    return Decimal(str(pstdev(returns) * 10000))


def _atr_bps(points: list[TradePoint], last: Decimal) -> Decimal:
    bars = _time_bars(points, seconds=60)
    if len(bars) < 2:
        return Decimal("0")
    true_ranges: list[Decimal] = []
    previous_close = bars[0][3]
    for _open, high, low, close in bars[1:]:
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = close
    return (
        sum(true_ranges, Decimal("0")) / Decimal(len(true_ranges)) / last * Decimal("10000")
        if true_ranges else Decimal("0")
    )


def _time_bars(
    points: list[TradePoint], *, seconds: int
) -> list[tuple[Decimal, Decimal, Decimal, Decimal]]:
    buckets: dict[int, list[Decimal]] = {}
    for point in sorted(points, key=lambda item: item.timestamp):
        key = int(point.timestamp.timestamp()) // seconds
        buckets.setdefault(key, []).append(point.price)
    return [
        (values[0], max(values), min(values), values[-1])
        for _, values in sorted(buckets.items())
    ]


def _time_bar_closes(points: list[TradePoint], *, seconds: int) -> list[Decimal]:
    return [bar[3] for bar in _time_bars(points, seconds=seconds)]


def _btc_relationship(
    symbol_points: list[TradePoint], btc_points: list[TradePoint], now: datetime,
) -> tuple[Decimal | None, Decimal | None]:
    def returns_by_bucket(points: list[TradePoint]) -> dict[int, Decimal]:
        rows = [
            point for point in points if now - point.timestamp <= timedelta(minutes=15)
        ]
        closes: dict[int, Decimal] = {}
        for point in sorted(rows, key=lambda item: item.timestamp):
            closes[int(point.timestamp.timestamp()) // 60] = point.price
        keys = sorted(closes)
        return {
            keys[index]: closes[keys[index]] / closes[keys[index - 1]] - Decimal("1")
            for index in range(1, len(keys))
            if closes[keys[index - 1]] > 0
        }

    symbol_returns = returns_by_bucket(symbol_points)
    btc_returns = returns_by_bucket(btc_points)
    keys = sorted(set(symbol_returns).intersection(btc_returns))
    if len(keys) < 3:
        return None, None
    xs = [float(btc_returns[key]) for key in keys]
    ys = [float(symbol_returns[key]) for key in keys]
    mean_x = sum(xs) / len(xs); mean_y = sum(ys) / len(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / len(xs)
    variance_x = sum((x - mean_x) ** 2 for x in xs) / len(xs)
    variance_y = sum((y - mean_y) ** 2 for y in ys) / len(ys)
    if variance_x <= 0 or variance_y <= 0:
        return None, None
    correlation = covariance / ((variance_x * variance_y) ** 0.5)
    beta = covariance / variance_x
    return Decimal(str(max(-1.0, min(1.0, correlation)))), Decimal(str(beta))


def _series_change(
    points: Sequence[tuple[datetime, Decimal]], now: datetime, window: timedelta,
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
