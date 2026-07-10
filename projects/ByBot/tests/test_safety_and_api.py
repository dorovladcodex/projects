import pytest
from pydantic import ValidationError

from app.config import Settings
from app.main import health, status


def test_live_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Live trading is blocked"):
        Settings(bot_mode="LIVE")


def test_health_and_status_report_live_disabled() -> None:
    assert health()["status"] == "ok"
    assert status()["live_trading"] is False
