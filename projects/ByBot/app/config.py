from __future__ import annotations

from enum import Enum
from functools import lru_cache
from decimal import Decimal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db.url import normalize_database_url


class BotMode(str, Enum):
    DATA_ONLY = "DATA_ONLY"
    PAPER = "PAPER"
    BYBIT_DEMO = "BYBIT_DEMO"


class ExecutionMode(str, Enum):
    PAPER = "PAPER"
    BYBIT_DEMO = "BYBIT_DEMO"


class MarketDataProviderName(str, Enum):
    MOCK = "MOCK"
    BYBIT_REST = "BYBIT_REST"


class BybitEnvironment(str, Enum):
    DEMO = "demo"
    MAINNET = "mainnet"


class NewsClassifierMode(str, Enum):
    MOCK = "mock"
    LLM = "llm"
    CODEX_CLI = "codex_cli"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_name: str = "bybot"
    app_env: str = "local"
    bot_mode: BotMode = BotMode.PAPER
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://bybot:bybot@localhost:5432/bybot"

    bybit_api_key: str | None = None
    bybit_api_secret: str | None = None
    bybit_env: BybitEnvironment = BybitEnvironment.DEMO
    bybit_enable_trading: bool = False
    bybit_demo_trading_enabled: bool = False
    bybit_live_trading_enabled: bool = False
    bybit_public_base_url: str = "https://api.bybit.com"
    bybit_private_demo_base_url: str = "https://api-demo.bybit.com"
    bybit_private_demo_ws_url: str = "wss://stream-demo.bybit.com"
    bybit_private_mainnet_base_url: str = "https://api.bybit.com"
    bybit_private_recv_window_ms: int = Field(default=5000, gt=0, le=60_000)
    bybit_private_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    bybit_account_refresh_interval_seconds: int = Field(default=30, ge=1, le=3600)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    llm_api_key: str | None = None
    news_classifier_mode: NewsClassifierMode = NewsClassifierMode.MOCK
    llm_provider_name: str = "openai-compatible"
    llm_api_url: str = "https://api.openai.com/v1/chat/completions"
    llm_model: str = "gpt-4.1-mini"
    llm_classifier_version: str = "news-v1"
    llm_allow_mock_fallback: bool = False
    llm_cache_ttl_seconds: int = Field(default=3600, ge=1, le=86400)
    llm_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_backoff_base_seconds: float = Field(default=0.5, ge=0, le=10)
    llm_rate_limit_per_minute: int = Field(default=20, ge=1, le=1000)
    llm_max_concurrent_requests: int = Field(default=2, ge=1, le=20)
    llm_circuit_breaker_failure_threshold: int = Field(default=5, ge=1, le=100)
    llm_circuit_breaker_cooldown_seconds: int = Field(default=60, ge=1, le=3600)
    llm_max_input_characters: int = Field(default=4000, ge=500, le=20000)
    llm_max_output_tokens: int = Field(default=250, ge=32, le=2000)
    llm_hourly_request_budget: int = Field(default=100, ge=1, le=100000)
    llm_daily_request_budget: int = Field(default=500, ge=1, le=1000000)
    llm_daily_token_budget: int = Field(default=100000, ge=100, le=100000000)
    codex_cli_enabled: bool = False
    codex_cli_path: str = "codex"
    codex_cli_model: str = "gpt-5.4-mini"
    codex_cli_fallback_model: str = "gpt-5.6-luna"
    codex_cli_reasoning_effort: str = "low"
    codex_cli_fallback_min_confidence: float = Field(default=0.75, ge=0, le=1)
    codex_cli_min_news_importance: float = Field(default=0.7, ge=0, le=1)

    news_poll_interval_seconds: int = Field(default=60, ge=10, le=3600)
    news_max_item_age_minutes: int = Field(default=60, ge=1, le=1440)
    news_min_importance_to_classify: float = Field(default=0.3, ge=0, le=1)
    news_enable_rss: bool = True
    news_rss_urls: tuple[str, ...] = ("https://cointelegraph.com/rss",)
    news_enable_mock_classifier: bool = True
    test_mode: bool = False
    auto_paper_execution: bool = False
    demo_risk_capital_usdt: Decimal = Field(default=Decimal("10000"), gt=0)
    demo_leverage: int = Field(default=1, ge=1, le=1)
    demo_order_link_prefix: str = Field(default="bybot", min_length=3, max_length=20)
    demo_run_id: str | None = Field(default=None, min_length=3, max_length=64)
    demo_reconciliation_interval_seconds: int = Field(default=15, ge=5, le=300)
    demo_order_confirmation_timeout_seconds: int = Field(default=30, ge=5, le=300)

    signal_min_classification_confidence: float = Field(default=0.80, ge=0, le=1)
    signal_min_news_importance: float = Field(default=0.70, ge=0, le=1)
    signal_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    signal_confirmation_window_seconds: int = Field(default=60, ge=1, le=600)
    signal_reevaluation_interval_seconds: int = Field(default=5, ge=1, le=60)
    signal_conflict_threshold_pct: float = Field(default=0.30, ge=0, le=10)
    signal_min_expected_edge_bps: float = Field(default=12.0, ge=0)
    signal_default_stop_loss_pct: float = Field(default=0.5, gt=0)
    signal_default_take_profit_pct: float = Field(default=1.0, gt=0)

    allowed_symbols: tuple[str, ...] = Field(
        default=("BTCUSDT", "ETHUSDT"),
        validation_alias=AliasChoices("ACTIVE_SYMBOLS", "ALLOWED_SYMBOLS", "allowed_symbols"),
    )
    market_data_provider: MarketDataProviderName = MarketDataProviderName.MOCK
    market_data_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    market_data_history_limit: int = Field(default=120, ge=2, le=2000)
    trading_paused: bool = False
    paper_starting_equity_usdt: float = Field(
        default=10_000.0,
        gt=0,
        validation_alias=AliasChoices(
            "PAPER_STARTING_EQUITY_USDT",
            "PAPER_STARTING_EQUITY",
            "paper_starting_equity_usdt",
            "paper_starting_equity",
        ),
    )

    @property
    def paper_starting_equity(self) -> float:
        """Backward-compatible alias; paper equity never comes from Bybit."""
        return self.paper_starting_equity_usdt
    paper_daily_pnl_pct: float = 0.0
    paper_weekly_pnl_pct: float = 0.0
    paper_consecutive_losses: int = Field(default=0, ge=0)
    paper_take_profit_pct: float = Field(default=1.0, gt=0)
    paper_position_timeout_minutes: int = Field(default=60, gt=0)
    paper_max_total_open_positions: int = Field(default=1, ge=1, le=10)
    paper_symbol_cooldown_seconds: int = Field(default=300, ge=0, le=86_400)
    paper_global_entry_cooldown_seconds: int = Field(default=60, ge=0, le=86_400)
    paper_max_daily_net_loss_pct: float = Field(default=2.0, gt=0, le=100)
    paper_max_weekly_net_loss_pct: float = Field(default=5.0, gt=0, le=100)
    paper_max_account_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
    default_paper_fees_bps: float = Field(default=6.0, ge=0)
    default_slippage_bps: float = Field(default=2.0, ge=0)
    paper_maker_fee_bps: float = Field(default=2.0, ge=0)
    paper_taker_fee_bps: float = Field(default=6.0, ge=0)
    paper_slippage_bps: float = Field(default=2.0, ge=0)
    min_net_edge_bps: float = Field(default=5.0, ge=0)
    max_position_notional_usdt: float = Field(default=5_000.0, gt=0)
    max_position_notional_pct_of_equity: float = Field(default=5.0, gt=0, le=100)
    min_position_notional_usdt: float = Field(default=10.0, gt=0)
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

    @field_validator("database_url", mode="before")
    @classmethod
    def select_psycopg_v3_driver(cls, value: object) -> object:
        return normalize_database_url(value) if isinstance(value, str) else value

    @field_validator("market_data_provider", mode="before")
    @classmethod
    def normalize_market_data_provider(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("bybit_env", mode="before")
    @classmethod
    def normalize_bybit_env(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("news_classifier_mode", mode="before")
    @classmethod
    def normalize_news_classifier_mode(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("execution_mode", mode="before")
    @classmethod
    def normalize_execution_mode(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("bybit_enable_trading")
    @classmethod
    def reject_bybit_trading_enabled(cls, value: bool) -> bool:
        if value:
            raise ValueError("Bybit order placement is blocked in Phase 3A")
        return value

    @field_validator("bybit_live_trading_enabled")
    @classmethod
    def reject_live_trading_enabled(cls, value: bool) -> bool:
        if value:
            raise ValueError("Bybit live trading is permanently unavailable")
        return value

    @field_validator("demo_order_link_prefix")
    @classmethod
    def validate_demo_order_link_prefix(cls, value: str) -> str:
        normalized = value.lower()
        if not normalized.replace("-", "").isalnum():
            raise ValueError("Demo order link prefix must be alphanumeric or hyphenated")
        return normalized

    @model_validator(mode="after")
    def enforce_demo_execution_guard(self) -> "Settings":
        if self.execution_mode != ExecutionMode.BYBIT_DEMO:
            return self
        errors: list[str] = []
        if self.app_env.lower() != "demo":
            errors.append("APP_ENV must be demo")
        if self.test_mode:
            errors.append("TEST_MODE must be false")
        if self.bot_mode != BotMode.BYBIT_DEMO:
            errors.append("BOT_MODE must be BYBIT_DEMO")
        if self.bybit_env != BybitEnvironment.DEMO:
            errors.append("BYBIT_ENV must be demo")
        if not self.bybit_demo_trading_enabled:
            errors.append("BYBIT_DEMO_TRADING_ENABLED must be true")
        if self.bybit_live_trading_enabled:
            errors.append("BYBIT_LIVE_TRADING_ENABLED must be false")
        if self.bybit_private_demo_base_url.rstrip("/") != "https://api-demo.bybit.com":
            errors.append("Demo REST domain must be exactly https://api-demo.bybit.com")
        if self.bybit_private_demo_ws_url.rstrip("/") != "wss://stream-demo.bybit.com":
            errors.append("Demo private WebSocket domain must be exactly wss://stream-demo.bybit.com")
        if self.auto_paper_execution:
            errors.append("AUTO_PAPER_EXECUTION must be false in BYBIT_DEMO mode")
        if self.demo_leverage != 1:
            errors.append("Demo leverage must be exactly 1")
        if not self.bybit_api_key or not self.bybit_api_secret:
            errors.append("Demo API credentials are required")
        if errors:
            raise ValueError("; ".join(errors))
        return self

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
