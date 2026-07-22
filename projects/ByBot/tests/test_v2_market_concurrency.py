from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event

import pytest

from app.config import Settings
from app.models import Symbol
from app.v2 import market as market_module
from app.v2.market import RollingFeatureEngine


def _engine(*, limit: int = 256) -> RollingFeatureEngine:
    return RollingFeatureEngine(Settings(
        _env_file=None,
        v2_enabled=True,
        v2_feature_history_limit=limit,
        v2_market_stale_seconds=300,
    ))


def _seed(engine: RollingFeatureEngine, symbol: Symbol) -> datetime:
    now = datetime.now(timezone.utc)
    engine.ingest_ticker(symbol, {"lastPrice": "100", "volume24h": "1000"}, now)
    engine.ingest_orderbook(
        symbol, [["99.9", "10"]], [["100.1", "10"]], now,
        snapshot=True, update_id=1, sequence=1,
    )
    engine.ingest_trade(symbol, Decimal("100"), Decimal("1"), "BUY", now)
    engine.ingest_rest_metrics(
        symbol, funding_rate=Decimal("0.0001"),
        open_interest=Decimal("1000"), volume_24h=Decimal("1000"),
        timestamp=now,
    )
    engine.mark_liquidation_subscribed((symbol,), at=now)
    return now


def _write_cycle(
    engine: RollingFeatureEngine, symbol: Symbol, start: datetime, index: int,
) -> None:
    stamp = start + timedelta(milliseconds=index)
    price = Decimal("100") + Decimal(index % 100) / Decimal("100")
    engine.ingest_ticker(symbol, {"lastPrice": str(price)}, stamp)
    engine.ingest_trade(
        symbol, price, Decimal("1"), "BUY" if index % 2 else "SELL", stamp
    )
    engine.ingest_orderbook(
        symbol,
        [[str(price - Decimal("0.1")), str(10 + index % 3)]],
        [[str(price + Decimal("0.1")), str(10 + index % 5)]],
        stamp, snapshot=True, update_id=index + 2, sequence=index + 2,
    )
    engine.ingest_liquidation(
        symbol, "BUY" if index % 2 else "SELL", price,
        Decimal("0.1"), stamp,
    )
    engine.ingest_rest_metrics(
        symbol, funding_rate=Decimal("0.0001"),
        open_interest=Decimal(1000 + index), volume_24h=Decimal("1000"),
        timestamp=stamp,
    )


def test_concurrent_producer_and_snapshot_reader_has_no_deque_runtime_error() -> None:
    engine = _engine()
    start = _seed(engine, Symbol.BTCUSDT)

    def produce() -> None:
        for index in range(2_000):
            _write_cycle(engine, Symbol.BTCUSDT, start, index)

    snapshots = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        producer = pool.submit(produce)
        reader = pool.submit(
            lambda: [engine.snapshot(Symbol.BTCUSDT, now=start + timedelta(seconds=1))
                     for _ in range(2_000)]
        )
        producer.result()
        snapshots = reader.result()

    assert all(item is not None for item in snapshots)
    assert len(engine.trades[Symbol.BTCUSDT]) <= 256
    assert len(engine.order_flow[Symbol.BTCUSDT]) <= 256
    assert len(engine.liquidations[Symbol.BTCUSDT]) <= 256
    assert len(engine.open_interest[Symbol.BTCUSDT]) <= 256


def test_repeated_mutation_during_feature_calculation_uses_immutable_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    start = _seed(engine, Symbol.BTCUSDT)
    captured = Event()
    release = Event()
    original = market_module._momentum

    def slow_momentum(points, last):
        captured.set()
        release.wait(timeout=2)
        return original(points, last)

    monkeypatch.setattr(market_module, "_momentum", slow_momentum)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(engine.snapshot, Symbol.BTCUSDT, now=start)
        assert captured.wait(timeout=2)
        for index in range(500):
            _write_cycle(engine, Symbol.BTCUSDT, start, index)
        release.set()
        snapshot = reader.result(timeout=5)
    assert snapshot is not None
    assert snapshot.observation_count["1h"] == 1


def test_multiple_symbols_can_produce_and_snapshot_in_parallel() -> None:
    engine = _engine()
    symbols = (Symbol.BTCUSDT, Symbol.ETHUSDT, Symbol.SOLUSDT, Symbol.NEARUSDT)
    start = {symbol: _seed(engine, symbol) for symbol in symbols}

    def exercise(symbol: Symbol) -> None:
        for index in range(250):
            _write_cycle(engine, symbol, start[symbol], index)
            assert engine.snapshot(symbol, now=start[symbol] + timedelta(seconds=1))

    with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
        futures = [pool.submit(exercise, symbol) for symbol in symbols]
        for future in futures:
            future.result(timeout=30)


def test_cancellation_while_waiting_for_snapshot_does_not_leave_lock_held() -> None:
    async def scenario() -> None:
        engine = _engine()
        now = _seed(engine, Symbol.BTCUSDT)
        locked = Event()
        release = Event()

        def hold_lock() -> None:
            with engine._state_lock:
                locked.set()
                release.wait(timeout=3)

        with ThreadPoolExecutor(max_workers=1) as pool:
            holder = pool.submit(hold_lock)
            assert await asyncio.to_thread(locked.wait, 2)
            task = asyncio.create_task(
                asyncio.to_thread(engine.snapshot, Symbol.BTCUSDT)
            )
            await asyncio.sleep(0)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            release.set()
            holder.result(timeout=3)
        await asyncio.sleep(0.05)
        engine.ingest_trade(
            Symbol.BTCUSDT, Decimal("101"), Decimal("1"), "BUY", now
        )
        assert engine.snapshot(Symbol.BTCUSDT, now=now) is not None

    asyncio.run(scenario())


def test_snapshot_is_internally_consistent_and_bounded() -> None:
    engine = _engine(limit=120)
    start = _seed(engine, Symbol.BTCUSDT)
    for index in range(100):
        _write_cycle(engine, Symbol.BTCUSDT, start, index)
    snapshot = engine.snapshot(Symbol.BTCUSDT, now=start + timedelta(seconds=1))
    assert snapshot is not None
    counts = snapshot.observation_count
    assert 0 <= counts["10s"] <= counts["30s"] <= counts["1m"] <= counts["1h"] <= 120
    assert snapshot.open_interest == Decimal("1099")
    assert snapshot.bid_price < snapshot.ask_price


def test_programming_exceptions_from_calculation_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    _seed(engine, Symbol.BTCUSDT)

    class CalculationBug(RuntimeError):
        pass

    def broken(*_args, **_kwargs):
        raise CalculationBug("feature implementation bug")

    monkeypatch.setattr(market_module, "_momentum", broken)
    with pytest.raises(CalculationBug, match="implementation bug"):
        engine.snapshot(Symbol.BTCUSDT)
