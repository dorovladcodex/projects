from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from app.models import MarketSnapshot, Symbol


class MarketDataProvider(Protocol):
    def get_snapshot(self, symbol: Symbol) -> MarketSnapshot: ...


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
        )
