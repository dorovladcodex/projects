from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class Asset(str, Enum):
    BTC = "BTC"
    ETH = "ETH"


class Symbol(str, Enum):
    BTCUSDT = "BTCUSDT"
    ETHUSDT = "ETHUSDT"


class Sentiment(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class SignalAction(str, Enum):
    TRADE = "TRADE"
    NO_TRADE = "NO_TRADE"


class NewsItem(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1000)
    source: str = Field(min_length=1, max_length=100)
    published_at: datetime
    asset_hint: Asset
    importance: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def timestamp_must_be_aware(self) -> "NewsItem":
        if self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        return self


class NewsClassification(BaseModel):
    news_id: UUID
    sentiment: Sentiment
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=300)
    model_name: str
    classified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MarketSnapshot(BaseModel):
    symbol: Symbol
    timestamp: datetime
    last_price: float = Field(gt=0)
    bid_price: float = Field(gt=0)
    ask_price: float = Field(gt=0)
    trend_score: float = Field(ge=-1, le=1)
    volatility_pct: float = Field(ge=0)
    liquidity_ok: bool
    api_stable: bool = True
    price_change_1m_pct: float = 0.0
    volume_24h: float | None = Field(default=None, ge=0)

    @property
    def spread_bps(self) -> float:
        midpoint = (self.ask_price + self.bid_price) / 2
        return (self.ask_price - self.bid_price) / midpoint * 10_000

    @property
    def spread_pct(self) -> float:
        return self.spread_bps / 100


class TradeSignal(BaseModel):
    action: SignalAction
    symbol: Symbol
    side: Side | None = None
    confidence: float = Field(ge=0, le=1)
    expected_edge_bps: float
    stop_loss_pct: float | None = Field(default=None, gt=0)
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RiskContext(BaseModel):
    equity: float = Field(gt=0)
    requested_risk_pct: float = Field(gt=0)
    leverage: int = Field(ge=1)
    open_positions: int = Field(ge=0)
    daily_pnl_pct: float
    weekly_pnl_pct: float
    consecutive_losses: int = Field(ge=0)
    api_stable: bool = True


class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    max_loss_amount: float = Field(default=0, ge=0)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PaperOrder(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    symbol: Symbol
    side: Side
    quantity: float = Field(gt=0)
    fill_price: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PaperTrade(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    order_id: UUID
    symbol: Symbol
    side: Side
    quantity: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    exit_price: float | None = Field(default=None, gt=0)
    realized_pnl: float = 0
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None


class Position(BaseModel):
    symbol: Symbol
    side: Side
    quantity: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    unrealized_pnl: float = 0


class BotEvent(BaseModel):
    event_type: str
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ErrorRecord(BaseModel):
    component: str
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
