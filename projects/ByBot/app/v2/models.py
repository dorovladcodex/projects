from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.models import ExecutionEnvironment, Symbol


class UniverseState(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class StrategySide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class StrategyName(str, Enum):
    NEWS_MOMENTUM_V2 = "NewsMomentumStrategyV2"
    VOLUME_BREAKOUT = "VolumeBreakoutStrategy"
    OI_FUNDING_SQUEEZE = "OIFundingSqueezeStrategy"
    LIQUIDATION_MOMENTUM = "LiquidationMomentumStrategy"
    MEME_TREND = "MemeTrendStrategy"
    RANGE_MEAN_REVERSION = "RangeMeanReversionStrategy"


class ReservationState(str, Enum):
    RESERVED = "RESERVED"
    EXECUTING = "EXECUTING"
    OPEN = "OPEN"
    RELEASED = "RELEASED"
    BLOCKED = "BLOCKED"


class SourceHealth(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"


class UniverseInstrument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Symbol
    exists: bool = True
    status: str
    category: str = "linear"
    settle_coin: str = "USDT"
    min_order_qty: Decimal = Field(gt=0)
    qty_step: Decimal = Field(gt=0)
    min_notional_value: Decimal = Field(ge=0)
    min_leverage: Decimal = Field(ge=1)
    max_leverage: Decimal = Field(ge=1)
    leverage_step: Decimal = Field(gt=0)
    tick_size: Decimal = Field(gt=0)
    turnover_24h: Decimal = Field(ge=0)
    spread_bps: Decimal = Field(ge=0)
    bid_depth_usdt: Decimal = Field(ge=0)
    ask_depth_usdt: Decimal = Field(ge=0)
    launch_time: datetime | None = None
    market_timestamp: datetime


class UniverseStatus(BaseModel):
    symbol: Symbol
    state: UniverseState
    accepted: bool
    reasons: list[str] = Field(default_factory=list)
    instrument: UniverseInstrument | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SourceState(BaseModel):
    source: str
    health: SourceHealth = SourceHealth.UNAVAILABLE
    connected: bool = False
    subscribed: bool = False
    subscription_confirmed_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_event_at: datetime | None = None
    last_message_at: datetime | None = None
    last_error: str | None = None
    reconnects: int = 0


class NewsModelUsage(BaseModel):
    """Run-scoped LLM usage; deterministic strategy adapters are excluded."""

    model_config = ConfigDict(extra="forbid")

    news_id: UUID
    model: str = Field(min_length=1, max_length=100)
    fallback_used: bool = False
    fallback_reason: str | None = Field(default=None, max_length=100)
    classified_at: datetime


class MarketFeatureSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Symbol
    timestamp: datetime
    fresh: bool
    stale_reasons: list[str] = Field(default_factory=list)
    last_price: Decimal = Field(gt=0)
    bid_price: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    spread_bps: Decimal = Field(ge=0)
    bid_depth_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    ask_depth_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    bid_depth_10bps_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    ask_depth_10bps_usdt: Decimal = Field(default=Decimal("0"), ge=0)
    price_momentum: dict[str, Decimal] = Field(default_factory=dict)
    breakout_distance_bps: dict[str, Decimal] = Field(default_factory=dict)
    volume_acceleration: dict[str, Decimal] = Field(default_factory=dict)
    trade_imbalance: dict[str, Decimal] = Field(default_factory=dict)
    order_flow_imbalance: dict[str, Decimal] = Field(default_factory=dict)
    orderbook_imbalance: Decimal = Decimal("0")
    microprice: Decimal | None = Field(default=None, gt=0)
    microprice_deviation_bps: Decimal = Decimal("0")
    realized_volatility: dict[str, Decimal] = Field(default_factory=dict)
    observation_count: dict[str, int] = Field(default_factory=dict)
    window_coverage_seconds: dict[str, Decimal] = Field(default_factory=dict)
    atr_bps: Decimal = Decimal("0")
    distance_from_high_bps: Decimal = Decimal("0")
    distance_from_low_bps: Decimal = Decimal("0")
    relative_strength_vs_btc_bps: Decimal = Decimal("0")
    rolling_correlation_vs_btc: Decimal | None = Field(default=None, ge=-1, le=1)
    btc_beta: Decimal | None = None
    funding_rate: Decimal | None = None
    funding_deviation_bps: Decimal | None = None
    open_interest: Decimal | None = None
    open_interest_change_pct: Decimal | None = None
    liquidation_long_usdt: Decimal = Decimal("0")
    liquidation_short_usdt: Decimal = Decimal("0")
    liquidation_imbalance: Decimal = Decimal("0")
    volume_24h: Decimal = Decimal("0")
    market_regime: str = "UNKNOWN"
    source_health: dict[str, SourceHealth] = Field(default_factory=dict)
    source_timestamps: dict[str, datetime | None] = Field(default_factory=dict)
    source_age_seconds: dict[str, float | None] = Field(default_factory=dict)
    stale_evidence: list[dict[str, Any]] = Field(default_factory=list)
    liquidation_last_valid_at: datetime | None = None
    liquidation_data_age_seconds: float | None = Field(default=None, ge=0)
    liquidation_data_valid: bool = True
    liquidation_feed_initialized: bool = False
    liquidation_feed_available: bool = True
    liquidation_connection_state: str = "DISCONNECTED"
    liquidation_subscription_state: str = "NOT_SUBSCRIBED"
    liquidation_event_count_5m: int = Field(default=0, ge=0)
    liquidation_notional_5m: Decimal = Field(default=Decimal("0"), ge=0)


class ScoreComponents(BaseModel):
    strategy_score: Decimal
    liquidity_score: Decimal
    market_confirmation_score: Decimal
    relative_strength_score: Decimal
    estimated_fee_penalty: Decimal
    estimated_slippage_penalty: Decimal
    correlation_penalty: Decimal
    portfolio_exposure_penalty: Decimal
    regime_score: Decimal = Decimal("0")
    uncertainty_penalty: Decimal = Decimal("0")
    final_score: Decimal


class V2SignalCandidate(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: str
    execution_environment: ExecutionEnvironment = ExecutionEnvironment.BYBIT_DEMO
    strategy_name: StrategyName
    strategy_version: str
    symbol: Symbol
    side: StrategySide
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    market_regime: str
    feature_snapshot: MarketFeatureSnapshot
    raw_strategy_score: Decimal
    confidence: Decimal = Field(ge=0, le=1)
    estimated_edge_bps: Decimal
    edge_proxy_bps: Decimal = Decimal("0")
    edge_calibrated: bool = False
    expected_funding_bps: Decimal = Decimal("0")
    expected_fees_bps: Decimal
    expected_slippage_bps: Decimal
    entry_reason: str
    rejection_reason: str | None = None
    setup_valid: bool = True
    setup_rejection_reasons: list[str] = Field(default_factory=list)
    threshold: Decimal
    distance_to_threshold: Decimal
    news_ids: list[str] = Field(default_factory=list)
    score_components: ScoreComponents | None = None
    rank_in_cycle: int | None = Field(default=None, ge=1)
    meta_label_probability: Decimal | None = Field(default=None, ge=0, le=1)
    meta_label_status: str = "UNCALIBRATED"
    admitted: bool = False
    state: str = "GENERATED"
    stop_loss_pct: Decimal = Field(gt=0)
    take_profit_pct: Decimal = Field(gt=0)
    trailing_stop_pct: Decimal | None = Field(default=None, gt=0)
    break_even_at_r: Decimal | None = Field(default=None, gt=0)
    maximum_holding_seconds: int = Field(gt=0)
    candidate_persisted_at: datetime | None = None
    reservation_requested_at: datetime | None = None
    reservation_created_at: datetime | None = None
    reservation_id: UUID | None = None
    risk_evaluation_started_at: datetime | None = None
    risk_approved_at: datetime | None = None
    execution_queue_entered_at: datetime | None = None
    execution_task_received_at: datetime | None = None
    execution_dispatched_at: datetime | None = None
    execution_rejected_at: datetime | None = None


class PortfolioReservation(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: str
    candidate_id: UUID
    symbol: Symbol
    strategy_name: StrategyName
    correlation_group: str
    notional_usdt: Decimal = Field(gt=0)
    risk_usdt: Decimal = Field(gt=0)
    side: StrategySide | None = None
    btc_beta: Decimal | None = None
    state: ReservationState = ReservationState.RESERVED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    released_at: datetime | None = None
    execution_id: UUID | None = None


class V2Incident(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    run_id: str
    event_type: str
    symbol: Symbol | None = None
    execution_id: UUID | None = None
    candidate_id: UUID | None = None
    error_category: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
