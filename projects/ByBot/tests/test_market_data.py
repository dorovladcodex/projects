from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.bybit.market_data import (
    BybitRestMarketDataClient,
    MarketDataService,
    snapshot_to_payload,
)
from app.bybit.private import build_account_service
from app.config import Settings
from app.models import MarketSnapshot, Symbol
from app.runtime import build_status


def test_bybit_rest_client_parses_public_ticker_response() -> None:
    def fake_http_get(url: str, timeout: float) -> dict[str, object]:
        assert "category=linear" in url
        assert "symbol=BTCUSDT" in url
        assert timeout == 5
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "lastPrice": "60000",
                        "bid1Price": "59999",
                        "ask1Price": "60001",
                        "volume24h": "12345.67",
                    }
                ],
            },
        }

    client = BybitRestMarketDataClient(timeout_seconds=5, http_get=fake_http_get)

    snapshot = client.get_snapshot(Symbol.BTCUSDT)

    assert snapshot.symbol == Symbol.BTCUSDT
    assert snapshot.last_price == 60_000
    assert snapshot.bid_price == 59_999
    assert snapshot.ask_price == 60_001
    assert snapshot.spread == 2
    assert snapshot.volume_24h == 12_345.67
    assert snapshot.spread_pct > 0


def test_market_data_service_keeps_safe_data_unavailable_status() -> None:
    class FailingProvider:
        def get_snapshot(self, symbol: Symbol) -> MarketSnapshot:
            raise ValueError("network unavailable")

    service = MarketDataService(FailingProvider(), [Symbol.BTCUSDT])

    service.refresh_all()

    payload = service.as_payload()
    assert payload["status"] == "DATA_UNAVAILABLE"
    assert "network unavailable" in payload["last_error"]
    assert payload["snapshots"] == []


def test_market_data_service_calculates_basic_metrics() -> None:
    class MovingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def get_snapshot(self, symbol: Symbol) -> MarketSnapshot:
            self.calls += 1
            price = 100 + self.calls
            return MarketSnapshot(
                symbol=symbol,
                timestamp=datetime.now(timezone.utc) + timedelta(seconds=self.calls),
                last_price=price,
                bid_price=price - 0.5,
                ask_price=price + 0.5,
                trend_score=0,
                volatility_pct=0,
                liquidity_ok=True,
            )

    service = MarketDataService(MovingProvider(), [Symbol.BTCUSDT])

    service.refresh_all()
    service.refresh_all()

    snapshot = service.latest_snapshots()[0]
    assert snapshot.price_change_1m_pct > 0
    assert snapshot.volatility_pct >= 0
    assert snapshot.simple_volatility >= 0
    assert snapshot.trend_score > 0
    assert snapshot.simple_trend.value in {"bullish", "sideways"}


def test_snapshot_payload_contains_phase_2_contract_fields() -> None:
    snapshot = MarketSnapshot(
        symbol=Symbol.BTCUSDT,
        timestamp=datetime.now(timezone.utc),
        last_price=60_000,
        bid_price=59_999,
        ask_price=60_001,
        trend_score=0,
        volatility_pct=0,
        liquidity_ok=True,
    )

    payload = snapshot_to_payload(snapshot)

    assert {
        "symbol",
        "last_price",
        "bid_price",
        "ask_price",
        "spread",
        "spread_pct",
        "price_change_1m_pct",
        "simple_trend",
        "simple_volatility",
        "volume_24h",
        "timestamp",
    } <= set(payload)


def test_status_blocks_trading_when_market_data_is_unavailable() -> None:
    class FailingProvider:
        def get_snapshot(self, symbol: Symbol) -> MarketSnapshot:
            raise ValueError("network unavailable")

    service = MarketDataService(FailingProvider(), [Symbol.BTCUSDT, Symbol.ETHUSDT])
    service.refresh_all()

    settings = Settings(bot_mode="PAPER", bybit_api_key=None, bybit_api_secret=None)
    payload = build_status(settings, service, build_account_service(settings))

    assert payload["market_data_status"] == "DATA_UNAVAILABLE"
    assert payload["trading_enabled"] is False
    assert payload["trading_blocked_data_unavailable"] is True
    assert payload["risk_status"]["state"] == "BLOCKED"
