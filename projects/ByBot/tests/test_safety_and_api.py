import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import health, market, status


def test_live_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Live trading is blocked"):
        Settings(bot_mode="LIVE")


def test_unsupported_symbols_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Unsupported symbols"):
        Settings(allowed_symbols=("SOLUSDT",))


def test_health_and_status_report_live_disabled() -> None:
    assert health()["status"] == "ok"
    payload = status()
    assert payload["live_trading"] is False
    assert payload["mode"] in {"DATA_ONLY", "PAPER", "BYBIT_DEMO"}
    assert isinstance(payload["trading_enabled"], bool)
    assert payload["trading_paused"] is False
    assert payload["active_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert payload["open_paper_position"] is None
    assert payload["last_signal"] is not None
    assert payload["market"]["status"] == "OK"
    assert payload["risk_status"]["state"] in {"OK", "BLOCKED"}


def test_market_endpoint_returns_snapshots() -> None:
    payload = market()

    assert payload["status"] == "OK"
    assert {item["symbol"] for item in payload["snapshots"]} == {"BTCUSDT", "ETHUSDT"}
    assert {"price", "bid", "ask", "spread_pct", "price_change_1m_pct"} <= set(
        payload["snapshots"][0]
    )
