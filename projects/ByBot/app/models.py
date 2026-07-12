from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


class Asset(str, Enum):
    BTC = "BTC"
    ETH = "ETH"
    MARKET = "MARKET"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


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


class NewsSignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class CandidateLifecycleState(str, Enum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    READY = "READY"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class SimpleTrend(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    SIDEWAYS = "sideways"
    UNKNOWN = "unknown"


class NewsItem(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=1000)
    source: str = Field(min_length=1, max_length=100)
    url: str | None = Field(default=None, max_length=2000)
    published_at: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    asset_hint: Asset = Asset.OTHER
    raw_category: str | None = Field(default=None, max_length=100)
    importance: float = Field(default=0.0, ge=0, le=1)

    @model_validator(mode="after")
    def timestamp_must_be_aware(self) -> "NewsItem":
        if self.published_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("news timestamps must be timezone-aware")
        return self


class NewsClassification(BaseModel):
    news_id: UUID
    asset: Asset = Asset.OTHER
    sentiment: Sentiment
    confidence: float = Field(ge=0, le=1)
    category: str = Field(default="general", max_length=100)
    urgency: str = Field(default="normal", max_length=30)
    reason: str = Field(default="", max_length=300)
    rationale: str = Field(default="", max_length=300)
    model_name: str
    classified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NewsFilterDebug(BaseModel):
    news_id: UUID
    title: str
    asset_hint: Asset
    importance: float = Field(ge=0, le=1)
    matched_keywords: list[str] = Field(default_factory=list)
    accepted: bool
    rejection_reasons: list[str] = Field(default_factory=list)


class MarketConfirmation(BaseModel):
    available: bool = False
    fresh: bool = False
    direction_confirmed: bool = False
    price_change_1m_pct: float | None = None
    trend_direction: str = "unknown"
    trend_score: float | None = None
    spread_bps: float | None = None
    volatility_pct: float | None = None
    volume_24h: float | None = None
    volume_change_pct: float | None = None
    volume_spike: bool | None = None
    reasons: list[str] = Field(default_factory=list)


class SignalEvaluation(BaseModel):
    evaluated_at: datetime
    price: float | None = Field(default=None, gt=0)
    price_change_1m_pct: float | None = None
    trend_direction: str = "unknown"
    volume_change_pct: float | None = None
    spread_bps: float | None = None
    volatility_pct: float | None = None
    market_confirmed: bool = False
    expected_edge_bps: float = 0.0
    state: CandidateLifecycleState
    reasons: list[str] = Field(default_factory=list)


class NewsSignalCandidate(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    news_id: UUID
    symbol: Symbol | None = None
    state: CandidateLifecycleState
    proposed_action: NewsSignalAction
    final_action: NewsSignalAction = NewsSignalAction.NO_TRADE
    sentiment: Sentiment
    classification_confidence: float = Field(ge=0, le=1)
    news_importance: float = Field(ge=0, le=1)
    category: str
    urgency: str
    market_confirmation: MarketConfirmation
    expected_edge_bps: float
    proposed_stop_loss_pct: float = Field(gt=0)
    proposed_take_profit_pct: float = Field(gt=0)
    ttl_seconds: int = Field(gt=0)
    reasons: list[str] = Field(default_factory=list)
    evaluation_history: list[SignalEvaluation] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime


class SignalTestFromNewsRequest(BaseModel):
    news_id: UUID
    reprocess: bool = False


class TestMarketSnapshotRequest(BaseModel):
    price: float = Field(gt=0)
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    price_change_1m_pct: float
    trend_direction: SimpleTrend
    trend_score: float = Field(ge=-1, le=1)
    volatility_pct: float = Field(ge=0)
    volume_24h: float | None = Field(default=None, ge=0)
    volume_change_pct: float | None = None
    volume_spike: bool | None = None
    timestamp: datetime | None = None
    fresh: bool = True

    @model_validator(mode="before")
    @classmethod
    def normalize_trend_direction(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("trend_direction"), str):
            data = dict(data)
            data["trend_direction"] = data["trend_direction"].lower()
        return data

    @model_validator(mode="after")
    def validate_test_snapshot(self) -> "TestMarketSnapshotRequest":
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        if self.timestamp is not None and self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return self


class MarketSnapshot(BaseModel):
    symbol: Symbol
    timestamp: datetime
    last_price: float = Field(gt=0)
    bid_price: float = Field(gt=0)
    ask_price: float = Field(gt=0)
    spread: float = Field(default=0, ge=0)
    spread_pct: float = Field(default=0, ge=0)
    price_change_1m_pct: float = 0.0
    simple_trend: SimpleTrend = SimpleTrend.UNKNOWN
    simple_volatility: float = Field(default=0, ge=0)
    volume_24h: float | None = Field(default=None, ge=0)
    trend_score: float = Field(ge=-1, le=1)
    volatility_pct: float = Field(ge=0)
    liquidity_ok: bool
    api_stable: bool = True

    @model_validator(mode="after")
    def calculate_spread_fields(self) -> "MarketSnapshot":
        self.spread = self.ask_price - self.bid_price
        midpoint = (self.ask_price + self.bid_price) / 2
        self.spread_pct = self.spread / midpoint * 100 if midpoint > 0 else 0
        return self

    @property
    def spread_bps(self) -> float:
        return self.spread_pct * 100


class TradeSignal(BaseModel):
    action: SignalAction
    symbol: Symbol
    side: Side | None = None
    confidence: float = Field(ge=0, le=1)
    expected_edge_bps: float
    stop_loss_pct: float | None = Field(default=None, gt=0)
    take_profit_pct: float | None = Field(default=None, gt=0)
    reasons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RiskContext(BaseModel):
    equity: float = Field(gt=0)
    available_balance: float | None = Field(default=None, ge=0)
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
    risk_based_size: float = Field(default=0, ge=0)
    capped_size: float = Field(default=0, ge=0)
    position_notional: float = Field(default=0, ge=0)
    max_allowed_notional: float = Field(default=0, ge=0)
    estimated_fees: float = Field(default=0, ge=0)
    estimated_slippage: float = Field(default=0, ge=0)
    size_was_capped: bool = False
    take_profit_bps: float = Field(default=0, ge=0)
    stop_loss_bps: float = Field(default=0, ge=0)
    round_trip_cost_bps: float = Field(default=0, ge=0)
    min_net_edge_bps: float = Field(default=0, ge=0)
    effective_expected_edge_bps: float = 0.0
    expected_net_edge_bps: float = 0.0
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SignalRiskPreview(BaseModel):
    preview_performed: bool = False
    preview_reason: str | None = None
    approved: bool | None = None
    capped_size: float = Field(default=0, ge=0)
    position_notional: float = Field(default=0, ge=0)
    max_allowed_notional: float = Field(default=0, ge=0)
    rejection_reasons: list[str] = Field(default_factory=list)


class SignalDryRunResult(BaseModel):
    candidate: NewsSignalCandidate
    risk_preview: SignalRiskPreview
    execution_attempted: bool = False
    paper_position_opened: bool = False


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


class PaperPosition(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    symbol: Symbol
    side: Side
    size: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    current_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    estimated_entry_fee: float = Field(default=0, ge=0)
    estimated_exit_fee: float = Field(default=0, ge=0)
    estimated_entry_slippage: float = Field(default=0, ge=0)
    estimated_exit_slippage: float = Field(default=0, ge=0)
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: datetime | None = None
    status: PositionStatus = PositionStatus.OPEN
    reason: str = "opened"


class PaperPnl(BaseModel):
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    open_positions: int = 0
    closed_trades: int = 0


class PaperTestSignalRequest(BaseModel):
    symbol: Symbol = Symbol.BTCUSDT
    side: Side = Side.BUY
    confidence: float = Field(default=0.9, ge=0, le=1)
    expected_edge_bps: float = 20.0
    stop_loss_pct: float = Field(default=0.5, gt=0)
    take_profit_pct: float | None = Field(default=None, gt=0)
    requested_risk_pct: float | None = Field(default=None, gt=0)
    leverage: int | None = Field(default=None, ge=1, le=2)


class BotEvent(BaseModel):
    event_type: str
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ErrorRecord(BaseModel):
    component: str
    message: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AccountPosition(BaseModel):
    symbol: Symbol
    side: str
    size: float = Field(ge=0)
    entry_price: float | None = Field(default=None, ge=0)
    mark_price: float | None = Field(default=None, ge=0)
    unrealized_pnl: float | None = None


class AccountOrder(BaseModel):
    symbol: Symbol
    order_id: str
    side: str | None = None
    order_type: str | None = None
    qty: float | None = Field(default=None, ge=0)
    price: float | None = Field(default=None, ge=0)
    order_status: str | None = None
    created_time: str | None = None


class AccountStatus(BaseModel):
    connected: bool = False
    environment: str
    trading_enabled: bool = False
    equity: float | None = Field(default=None, ge=0)
    available_balance: float | None = Field(default=None, ge=0)
    open_positions: list[AccountPosition] = Field(default_factory=list)
    open_orders: list[AccountOrder] = Field(default_factory=list)
    recent_closed_orders: list[AccountOrder] = Field(default_factory=list)
    stale: bool = False
    last_error: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_refresh_attempt_at: datetime | None = None
