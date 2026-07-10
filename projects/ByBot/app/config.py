from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotMode(str, Enum):
    DATA_ONLY = "DATA_ONLY"
    PAPER = "PAPER"
    BYBIT_DEMO = "BYBIT_DEMO"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_name: str = "bybot"
    bot_mode: BotMode = BotMode.PAPER
    log_level: str = "INFO"
    database_url: str = "postgresql://bybot:bybot@localhost:5432/bybot"

    bybit_api_key: str | None = None
    bybit_api_secret: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    llm_api_key: str | None = None

    allowed_symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
    trading_paused: bool = False
    paper_starting_equity: float = Field(default=10_000.0, gt=0)
    paper_daily_pnl_pct: float = 0.0
    paper_weekly_pnl_pct: float = 0.0
    paper_consecutive_losses: int = Field(default=0, ge=0)
    max_risk_per_trade_pct: float = Field(default=0.5, gt=0, le=0.5)
    max_daily_loss_pct: float = Field(default=2.0, gt=0, le=2.0)
    max_weekly_loss_pct: float = Field(default=5.0, gt=0, le=5.0)
    max_leverage: int = Field(default=2, ge=1, le=2)
    max_spread_bps: float = Field(default=8.0, gt=0)
    min_llm_confidence: float = Field(default=0.70, ge=0, le=1)
    min_expected_edge_bps: float = Field(default=12.0, gt=0)

    @field_validator("bot_mode", mode="before")
    @classmethod
    def reject_unsupported_modes(cls, value: object) -> object:
        if isinstance(value, str) and value.upper() == "LIVE":
            raise ValueError("Live trading is blocked in v1")
        return value.upper() if isinstance(value, str) else value

    @field_validator("allowed_symbols")
    @classmethod
    def restrict_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        supported = {"BTCUSDT", "ETHUSDT"}
        symbols = tuple(symbol.upper() for symbol in value)
        if not symbols:
            raise ValueError("At least one active symbol is required")
        unsupported = sorted(set(symbols) - supported)
        if unsupported:
            raise ValueError(f"Unsupported symbols in v1: {', '.join(unsupported)}")
        return symbols


@lru_cache
def get_settings() -> Settings:
    return Settings()
