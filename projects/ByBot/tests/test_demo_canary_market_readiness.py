from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bybit.canary_market import (
    CanaryMarketObservation,
    CanaryMarketReadinessError,
    wait_for_canary_market_data,
)
from app.bybit.demo import DemoExecutionService
from app.bybit.market_data import MarketDataService
from app.config import Settings
from app.models import MarketSnapshot, Symbol


class FakeTime:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.base = datetime(2026, 7, 25, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self.base + timedelta(seconds=self.elapsed)

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


def _observation(
    fake: FakeTime,
    *,
    source: str,
    age_seconds: float = 0,
    future_seconds: float = 0,
) -> CanaryMarketObservation:
    timestamp = fake.now() - timedelta(seconds=age_seconds) + timedelta(
        seconds=future_seconds
    )
    return CanaryMarketObservation(
        snapshot=MarketSnapshot(
            symbol=Symbol.XRPUSDT,
            timestamp=timestamp,
            last_price=2.4,
            bid_price=2.39,
            ask_price=2.4,
            trend_score=0,
            volatility_pct=0,
            liquidity_ok=True,
        ),
        source=source,
        exchange_timestamp=timestamp,
        received_at=fake.now(),
    )


def _wait(
    fake: FakeTime,
    *,
    websocket_provider,
    rest_provider,
    timeout: float = 2,
    warmup: float = 0.5,
):
    return wait_for_canary_market_data(
        Symbol.XRPUSDT,
        accepted_symbols=(Symbol.XRPUSDT,),
        websocket_provider=websocket_provider,
        rest_provider=rest_provider,
        timeout_seconds=timeout,
        websocket_warmup_seconds=warmup,
        freshness_seconds=15,
        clock=fake.now,
        monotonic_clock=fake.monotonic,
        sleeper=fake.sleep,
        poll_seconds=0.25,
    )


def test_preview_waits_for_fresh_websocket_snapshot() -> None:
    fake = FakeTime()
    attempts = 0

    def websocket(_symbol):
        nonlocal attempts
        attempts += 1
        return _observation(fake, source="WS") if attempts == 3 else None

    result = _wait(
        fake, websocket_provider=websocket,
        rest_provider=lambda _symbol: pytest.fail("REST fallback was premature"),
    )
    assert result.observation.source == "WS"
    assert result.waited_seconds == pytest.approx(0.5)


def test_preview_succeeds_with_fresh_rest_fallback() -> None:
    fake = FakeTime()
    result = _wait(
        fake,
        websocket_provider=lambda _symbol: None,
        rest_provider=lambda _symbol: _observation(fake, source="REST"),
    )
    assert result.observation.source == "REST"
    assert result.age_seconds == 0
    assert result.waited_seconds >= 0.5


def test_stale_snapshot_is_rejected_with_bounded_timeout() -> None:
    fake = FakeTime()
    with pytest.raises(CanaryMarketReadinessError) as error:
        _wait(
            fake,
            websocket_provider=None,
            rest_provider=lambda _symbol: _observation(
                fake, source="REST", age_seconds=60
            ),
            timeout=1,
            warmup=0,
        )
    assert error.value.report["reason"] == "market snapshot is stale"
    assert error.value.report["waited_seconds"] == pytest.approx(1)


def test_future_timestamp_is_rejected_as_clock_error() -> None:
    fake = FakeTime()
    with pytest.raises(CanaryMarketReadinessError) as error:
        _wait(
            fake,
            websocket_provider=None,
            rest_provider=lambda _symbol: _observation(
                fake, source="REST", future_seconds=5
            ),
            timeout=0.5,
            warmup=0,
        )
    assert error.value.report["reason"] == "market timestamp is in the future"


def test_timeout_has_no_candidate_or_exchange_mutation() -> None:
    fake = FakeTime()
    calls = {"market_reads": 0, "mutations": 0}

    def rest(_symbol):
        calls["market_reads"] += 1
        return None

    with pytest.raises(CanaryMarketReadinessError):
        _wait(
            fake, websocket_provider=None, rest_provider=rest,
            timeout=0.5, warmup=0,
        )
    assert calls["market_reads"] > 0
    assert calls["mutations"] == 0


def test_symbol_unavailable_on_demo_fails_before_market_read() -> None:
    calls = 0

    def rest(_symbol):
        nonlocal calls
        calls += 1
        return None

    with pytest.raises(CanaryMarketReadinessError) as error:
        wait_for_canary_market_data(
            Symbol.XRPUSDT,
            accepted_symbols=(Symbol.BTCUSDT,),
            websocket_provider=None,
            rest_provider=rest,
            timeout_seconds=1,
            websocket_warmup_seconds=0,
            freshness_seconds=15,
        )
    assert error.value.report["reason"] == "symbol_not_available_on_demo"
    assert calls == 0


def test_symbol_specific_rest_refresh_is_not_limited_by_startup_symbols() -> None:
    class Provider:
        def __init__(self) -> None:
            self.symbols: list[Symbol] = []

        def get_snapshot(self, symbol: Symbol) -> MarketSnapshot:
            self.symbols.append(symbol)
            return MarketSnapshot(
                symbol=symbol,
                timestamp=datetime.now(timezone.utc),
                last_price=2.4,
                bid_price=2.39,
                ask_price=2.4,
                trend_score=0,
                volatility_pct=0,
                liquidity_ok=True,
            )

    provider = Provider()
    service = MarketDataService(provider, (Symbol.BTCUSDT,))
    snapshot = service.refresh_symbol(Symbol.XRPUSDT)
    assert snapshot.symbol == Symbol.XRPUSDT
    assert provider.symbols == [Symbol.XRPUSDT]


class _StatusRepository:
    def load_demo_kill_switch(self):
        return {
            "active": False,
            "reasons": ["historical protection incident"],
        }

    def load_demo_executions(self):
        return []


def test_demo_status_separates_active_and_historical_kill_switch_reasons() -> None:
    service = DemoExecutionService(
        Settings(_env_file=None), _StatusRepository(), None
    )
    status = service.as_status()
    assert status["kill_switch_active"] is False
    assert status["active_kill_switch_reasons"] == []
    assert status["historical_kill_switch_reasons"] == [
        "historical protection incident"
    ]

    service.kill_switch_active = True
    active = service.as_status()
    assert active["active_kill_switch_reasons"] == [
        "historical protection incident"
    ]
