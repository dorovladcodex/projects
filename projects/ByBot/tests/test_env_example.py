from __future__ import annotations

from pathlib import Path


def test_env_example_contains_required_phase_1_variables() -> None:
    content = Path(".env.example").read_text(encoding="utf-8")
    keys = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in content.splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    required = {
        "BOT_NAME",
        "BOT_MODE",
        "LOG_LEVEL",
        "DATABASE_URL",
        "BYBIT_API_KEY",
        "BYBIT_API_SECRET",
        "BYBIT_ENV",
        "BYBIT_ENABLE_TRADING",
        "BYBIT_PUBLIC_BASE_URL",
        "BYBIT_PRIVATE_DEMO_BASE_URL",
        "BYBIT_PRIVATE_MAINNET_BASE_URL",
        "BYBIT_PRIVATE_RECV_WINDOW_MS",
        "BYBIT_PRIVATE_TIMEOUT_SECONDS",
        "BYBIT_ACCOUNT_REFRESH_INTERVAL_SECONDS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "LLM_API_KEY",
        "ALLOWED_SYMBOLS",
        "MARKET_DATA_PROVIDER",
        "MARKET_DATA_TIMEOUT_SECONDS",
        "MARKET_DATA_HISTORY_LIMIT",
        "TRADING_PAUSED",
        "PAPER_STARTING_EQUITY",
        "PAPER_DAILY_PNL_PCT",
        "PAPER_WEEKLY_PNL_PCT",
        "PAPER_CONSECUTIVE_LOSSES",
        "PAPER_TAKE_PROFIT_PCT",
        "PAPER_POSITION_TIMEOUT_MINUTES",
        "DEFAULT_PAPER_FEES_BPS",
        "DEFAULT_SLIPPAGE_BPS",
        "MIN_NET_EDGE_BPS",
        "MAX_POSITION_NOTIONAL_USDT",
        "MAX_POSITION_NOTIONAL_PCT_OF_EQUITY",
        "MIN_POSITION_NOTIONAL_USDT",
        "MAX_RISK_PER_TRADE_PCT",
        "MAX_DAILY_LOSS_PCT",
        "MAX_WEEKLY_LOSS_PCT",
        "MAX_LEVERAGE",
        "MAX_SPREAD_BPS",
        "MIN_LLM_CONFIDENCE",
        "MIN_EXPECTED_EDGE_BPS",
    }

    assert required <= set(keys)
    assert keys["BOT_MODE"] != "LIVE"
    assert all(keys[key] for key in required)
