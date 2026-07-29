from __future__ import annotations

import os

import pytest


# Pytest imports several API test modules at collection time.  Establish the
# fail-closed test process before any of those modules can import app.main; do
# not inherit mutation-capable values from a developer shell or local .env.
_SAFE_TEST_ENV = {
    "APP_ENV": "test",
    "BOT_MODE": "PAPER",
    "EXECUTION_MODE": "PAPER",
    "TEST_MODE": "true",
    "BYBIT_ENV": "demo",
    "BYBIT_ENABLE_TRADING": "false",
    "BYBIT_DEMO_TRADING_ENABLED": "false",
    "DEMO_ORDER_EXECUTION_AUTHORIZED": "false",
    "BYBIT_LIVE_TRADING_ENABLED": "false",
    "DEMO_CANARY_ENABLED": "false",
    "V2_ENABLED": "false",
    "V2_AUTO_DEMO_EXECUTION": "false",
    "AUTO_PAPER_EXECUTION": "false",
    "MARKET_DATA_PROVIDER": "MOCK",
    "NEWS_CLASSIFIER_MODE": "mock",
    "NEWS_ENABLE_RSS": "false",
    "DATABASE_URL": "sqlite://",
    "ALLOWED_SYMBOLS": '["BTCUSDT","ETHUSDT"]',
}

os.environ.update(_SAFE_TEST_ENV)
os.environ.pop("BYBIT_API_KEY", None)
os.environ.pop("BYBIT_API_SECRET", None)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
