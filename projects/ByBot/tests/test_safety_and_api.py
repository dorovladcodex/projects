import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.main as main_module
from app.bybit.market_data import build_market_data_service
from app.bybit.private import build_account_service
from app.config import MarketDataProviderName, Settings


main_module.market_data_service = build_market_data_service(
    Settings(market_data_provider=MarketDataProviderName.MOCK)
)
main_module.account_service = build_account_service(
    Settings(bybit_api_key=None, bybit_api_secret=None)
)


def test_live_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Live trading is blocked"):
        Settings(bot_mode="LIVE")


def test_unsupported_symbols_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Unsupported symbols"):
        Settings(allowed_symbols=("SOLUSDT",))


def test_bybit_enable_trading_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Bybit order placement is blocked"):
        Settings(bybit_enable_trading=True)


def test_health_and_status_report_live_disabled() -> None:
    assert main_module.health()["status"] == "ok"
    payload = main_module.status()
    assert payload["live_trading"] is False
    assert payload["mode"] in {"DATA_ONLY", "PAPER", "BYBIT_DEMO"}
    assert isinstance(payload["trading_enabled"], bool)
    assert payload["trading_paused"] is False
    assert payload["active_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert payload["open_paper_position"] is None
    assert payload["last_signal"] is not None
    assert payload["market"]["status"] == "OK"
    assert payload["market_data_status"] == "OK"
    assert payload["latest_btcusdt_snapshot"] is not None
    assert payload["latest_ethusdt_snapshot"] is not None
    assert payload["trading_blocked_data_unavailable"] is False
    assert payload["private_api_connected"] is False
    assert payload["order_placement_blocked"] is True
    assert payload["account"]["trading_enabled"] is False
    assert payload["risk_status"]["state"] in {"OK", "BLOCKED"}


def test_market_endpoint_returns_snapshots() -> None:
    payload = main_module.market()

    assert payload["status"] == "OK"
    assert {item["symbol"] for item in payload["snapshots"]} == {"BTCUSDT", "ETHUSDT"}
    assert {
        "last_price",
        "bid_price",
        "ask_price",
        "spread",
        "spread_pct",
        "price_change_1m_pct",
        "simple_trend",
        "simple_volatility",
    } <= set(payload["snapshots"][0])


def test_market_symbol_endpoint_returns_single_snapshot() -> None:
    payload = main_module.market_symbol("BTCUSDT")

    assert payload["status"] == "OK"
    assert payload["snapshot"]["symbol"] == "BTCUSDT"


def test_market_symbol_endpoint_rejects_unsupported_symbol() -> None:
    with pytest.raises(HTTPException) as exc:
        main_module.market_symbol("SOLUSDT")
    assert exc.value.status_code == 404


def test_account_endpoint_returns_safe_disconnected_status() -> None:
    payload = main_module.account()

    assert payload["connected"] is False
    assert payload["environment"] == "demo"
    assert payload["trading_enabled"] is False
