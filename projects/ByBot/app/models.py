from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Asset(str, Enum):
    BTC = "BTC"
    ETH = "ETH"
    MARKET = "MARKET"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class LLMAsset(str, Enum):
    BTC = "BTC"
    ETH = "ETH"
    MARKET = "MARKET"
    OTHER = "OTHER"


class Symbol(str, Enum):
    BTCUSDT = "BTCUSDT"
    ETHUSDT = "ETHUSDT"
    SOLUSDT = "SOLUSDT"
    XRPUSDT = "XRPUSDT"
    DOGEUSDT = "DOGEUSDT"
    ADAUSDT = "ADAUSDT"
    LINKUSDT = "LINKUSDT"
    AVAXUSDT = "AVAXUSDT"
    SUIUSDT = "SUIUSDT"
    NEARUSDT = "NEARUSDT"
    LTCUSDT = "LTCUSDT"
    TONUSDT = "TONUSDT"
    PEPEUSDT = "PEPEUSDT"
    SHIBUSDT = "SHIBUSDT"
    WIFUSDT = "WIFUSDT"
    BONKUSDT = "BONKUSDT"
    FLOKIUSDT = "FLOKIUSDT"


class Sentiment(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class ClassificationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    CACHE_HIT = "CACHE_HIT"
    FAILED = "FAILED"
    FALLBACK_MOCK = "FALLBACK_MOCK"


class NewsCategory(str, Enum):
    ETF = "etf"
    REGULATION = "regulation"
    SECURITY = "security"
    MACRO = "macro"
    EXCHANGE = "exchange"
    LISTING = "listing"
    ADOPTION = "adoption"
    FUND_FLOW = "fund_flow"
    OTHER = "other"


class NewsUrgency(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


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


class ExecutionEnvironment(str, Enum):
    """Execution venue attached to durable candidates and executions."""

    PAPER = "PAPER"
    BYBIT_DEMO = "BYBIT_DEMO"


class CandidateLifecycleState(str, Enum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    READY = "READY"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    EXECUTING_PAPER = "EXECUTING_PAPER"
    PAPER_OPENED = "PAPER_OPENED"
    PAPER_CLOSED = "PAPER_CLOSED"
    EXECUTION_BLOCKED = "EXECUTION_BLOCKED"
    DEMO_SUBMITTING = "DEMO_SUBMITTING"
    DEMO_ORDER_ACKNOWLEDGED = "DEMO_ORDER_ACKNOWLEDGED"
    DEMO_ACCEPTED = "DEMO_ACCEPTED"
    DEMO_PARTIALLY_FILLED = "DEMO_PARTIALLY_FILLED"
    DEMO_FILLED = "DEMO_FILLED"
    DEMO_FULLY_FILLED = "DEMO_FULLY_FILLED"
    DEMO_PROTECTION_PENDING = "DEMO_PROTECTION_PENDING"
    DEMO_POSITION_OPEN = "DEMO_POSITION_OPEN"
    DEMO_CLOSING = "DEMO_CLOSING"
    DEMO_CLOSED = "DEMO_CLOSED"
    DEMO_CLOSED_AFTER_FAILURE = "DEMO_CLOSED_AFTER_FAILURE"
    DEMO_FAILED = "DEMO_FAILED"
    DEMO_RECONCILIATION_REQUIRED = "DEMO_RECONCILIATION_REQUIRED"
    DEMO_NOT_SUBMITTED = "DEMO_NOT_SUBMITTED"
    DEMO_ORDER_CANCELLED = "DEMO_ORDER_CANCELLED"
    DEMO_CLOSED_AFTER_INTERRUPTION = "DEMO_CLOSED_AFTER_INTERRUPTION"
    DEMO_CLOSED_EXTERNALLY = "DEMO_CLOSED_EXTERNALLY"
    DEMO_FAILED_FLAT_VERIFIED = "DEMO_FAILED_FLAT_VERIFIED"


class DemoExecutionState(str, Enum):
    DEMO_SUBMITTING = "DEMO_SUBMITTING"
    DEMO_ORDER_ACKNOWLEDGED = "DEMO_ORDER_ACKNOWLEDGED"
    DEMO_ACCEPTED = "DEMO_ACCEPTED"
    DEMO_PARTIALLY_FILLED = "DEMO_PARTIALLY_FILLED"
    DEMO_FILLED = "DEMO_FILLED"
    DEMO_FULLY_FILLED = "DEMO_FULLY_FILLED"
    DEMO_PROTECTION_PENDING = "DEMO_PROTECTION_PENDING"
    DEMO_POSITION_OPEN = "DEMO_POSITION_OPEN"
    DEMO_CLOSING = "DEMO_CLOSING"
    DEMO_CLOSED = "DEMO_CLOSED"
    DEMO_CLOSED_AFTER_FAILURE = "DEMO_CLOSED_AFTER_FAILURE"
    DEMO_FAILED = "DEMO_FAILED"
    DEMO_RECONCILIATION_REQUIRED = "DEMO_RECONCILIATION_REQUIRED"
    DEMO_NOT_SUBMITTED = "DEMO_NOT_SUBMITTED"
    DEMO_ORDER_CANCELLED = "DEMO_ORDER_CANCELLED"
    DEMO_CLOSED_AFTER_INTERRUPTION = "DEMO_CLOSED_AFTER_INTERRUPTION"
    DEMO_CLOSED_EXTERNALLY = "DEMO_CLOSED_EXTERNALLY"
    DEMO_FAILED_FLAT_VERIFIED = "DEMO_FAILED_FLAT_VERIFIED"


class DemoFill(BaseModel):
    execution_id: str
    order_id: str
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Decimal("0")
    fee_currency: str | None = None
    executed_at: datetime
    local_received_at: datetime | None = None


class DemoExecutionRecord(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    candidate_id: UUID
    risk_decision_id: int | None = None
    run_id: str
    execution_environment: ExecutionEnvironment = ExecutionEnvironment.BYBIT_DEMO
    order_link_id: str
    state: DemoExecutionState
    symbol: Symbol
    side: Side
    requested_quantity: Decimal = Field(gt=0)
    leverage: Decimal = Field(default=Decimal("1"), ge=1)
    strategy_name: str | None = None
    strategy_version: str | None = None
    reference_entry_price: Decimal | None = Field(default=None, gt=0)
    accepted_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    average_fill_price: Decimal | None = Field(default=None, gt=0)
    exchange_order_status: str | None = None
    order_id: str | None = None
    fills: list[DemoFill] = Field(default_factory=list)
    exchange_fees: Decimal = Decimal("0")
    take_profit: Decimal | None = None
    stop_loss: Decimal | None = None
    stop_loss_pct: Decimal | None = Field(default=None, gt=0)
    take_profit_pct: Decimal | None = Field(default=None, gt=0)
    trailing_stop_pct: Decimal | None = Field(default=None, gt=0)
    break_even_at_r: Decimal | None = Field(default=None, gt=0)
    maximum_holding_seconds: int | None = Field(default=None, gt=0)
    position_data_stale_since: datetime | None = None
    position_data_stale_feature: str | None = None
    position_data_stale_age_seconds: float | None = Field(default=None, ge=0)
    position_data_stale_threshold_seconds: float | None = Field(default=None, gt=0)
    position_data_stale_protection_confirmed: bool | None = None
    tp_identifier: str | None = None
    sl_identifier: str | None = None
    tp_order_id: str | None = None
    sl_order_id: str | None = None
    protection_position_idx: int = 0
    protection_orders_verified_at: datetime | None = None
    protection_confirmed: bool = False
    close_order_link_id: str | None = None
    close_order_id: str | None = None
    close_fills: list[DemoFill] = Field(default_factory=list)
    average_close_price: Decimal | None = Field(default=None, gt=0)
    gross_realized_pnl: Decimal | None = None
    realized_exchange_pnl: Decimal | None = None
    maximum_favorable_excursion: Decimal = Decimal("0")
    maximum_adverse_excursion: Decimal = Decimal("0")
    entry_slippage: Decimal | None = None
    exit_slippage: Decimal | None = None
    paper_shadow_pnl: Decimal | None = None
    close_reason: str | None = None
    exit_attribution: str | None = None
    exit_attribution_evidence: dict[str, Any] = Field(default_factory=dict)
    attribution_failure_reason: str | None = None
    failure_reason: str | None = None
    cleanup_result: str | None = None
    last_reconciliation_at: datetime | None = None
    terminalization_warning_at: datetime | None = None
    terminalization_hard_failure_at: datetime | None = None
    terminalization_blockers: list[str] = Field(default_factory=list)
    last_error: str | None = None
    signal_created_at: datetime | None = None
    candidate_persisted_at: datetime | None = None
    reservation_requested_at: datetime | None = None
    reservation_created_at: datetime | None = None
    risk_evaluation_started_at: datetime | None = None
    risk_approved_at: datetime | None = None
    execution_dispatched_at: datetime | None = None
    execution_task_received_at: datetime | None = None
    ownership_check_started_at: datetime | None = None
    ownership_check_completed_at: datetime | None = None
    reconciliation_check_started_at: datetime | None = None
    reconciliation_check_completed_at: datetime | None = None
    account_verification_started_at: datetime | None = None
    account_verification_completed_at: datetime | None = None
    position_query_started_at: datetime | None = None
    position_query_completed_at: datetime | None = None
    open_orders_query_started_at: datetime | None = None
    open_orders_query_completed_at: datetime | None = None
    instrument_metadata_started_at: datetime | None = None
    instrument_metadata_completed_at: datetime | None = None
    leverage_setup_started_at: datetime | None = None
    leverage_setup_completed_at: datetime | None = None
    quantity_normalization_started_at: datetime | None = None
    quantity_normalization_completed_at: datetime | None = None
    protection_plan_started_at: datetime | None = None
    protection_plan_completed_at: datetime | None = None
    database_execution_state_started_at: datetime | None = None
    database_execution_state_completed_at: datetime | None = None
    exchange_submit_started_at: datetime | None = None
    execution_stage_durations_ms: dict[str, float] = Field(default_factory=dict)
    order_submitted_at: datetime | None = None
    order_acknowledged_at: datetime | None = None
    exchange_order_created_at: datetime | None = None
    exchange_fill_at: datetime | None = None
    local_submit_started_at: datetime | None = None
    local_ack_received_at: datetime | None = None
    local_fill_received_at: datetime | None = None
    fill_before_ack: bool = False
    order_submit_to_first_fill_ms: float | None = Field(default=None, ge=0)
    ack_to_first_fill_ms: float | None = Field(default=None, ge=0)
    first_fill_at: datetime | None = None
    position_confirmed_at: datetime | None = None
    protection_confirmed_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


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
    classification_status: ClassificationStatus = ClassificationStatus.SUCCESS
    trade_eligible: bool = True
    eligibility_reasons: list[str] = Field(default_factory=list)
    provider_name: str = "mock"
    classifier_version: str = "mock-v1"
    latency_ms: float = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_input_tokens: int = Field(default=0, ge=0)
    estimated_output_tokens: int = Field(default=0, ge=0)
    codex_cli_total_tokens: int | None = Field(default=None, ge=0)
    codex_cli_token_count_available: bool = False
    cache_hit: bool = False
    error_code: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = Field(default=None, max_length=100)
    request_attempt_number: int = Field(default=1, ge=0)
    failure_category: str | None = Field(default=None, max_length=100)
    classified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LLMClassificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: LLMAsset
    sentiment: Sentiment
    confidence: float = Field(ge=0, le=1)
    category: NewsCategory
    urgency: NewsUrgency
    reason: str = Field(min_length=1, max_length=250)



class ClassifierTestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)


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
    execution_environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
    run_id: str | None = None
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


class DemoCanaryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: Symbol = Symbol.BTCUSDT
    max_notional_usdt: Decimal = Field(gt=0)
    market_price_buffer_pct: Decimal | None = Field(default=None, ge=0, le=100)


class DemoCanaryExecuteRequest(DemoCanaryPreviewRequest):
    expected_rules_fingerprint: str = Field(min_length=64, max_length=64)


class DemoCanaryFailureCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=250)


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
    risk_decision_id: int | None = None
    estimated_fees: float = Field(default=0, ge=0)
    estimated_slippage: float = Field(default=0, ge=0)


class SignalDryRunResult(BaseModel):
    candidate: NewsSignalCandidate
    risk_preview: SignalRiskPreview
    execution_attempted: bool = False
    paper_position_opened: bool = False
    execution_block_reason: str | None = None
    execution_error_code: str | None = None
    execution_retryable: bool = False
    demo_execution: dict[str, object] | None = None
    canary_plan: dict[str, str] | None = None


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
    execution_environment: ExecutionEnvironment = ExecutionEnvironment.PAPER
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
    candidate_id: UUID | None = None
    risk_decision_id: int | None = None
    execution_key: str | None = None
    position_notional: float = Field(default=0, ge=0)
    gross_pnl: float = 0.0
    fees_paid: float = Field(default=0, ge=0)
    slippage_paid: float = Field(default=0, ge=0)
    close_reason: str | None = None


class PaperMarketSnapshotTestRequest(BaseModel):
    symbol: Symbol
    price: float = Field(gt=0)
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    timestamp: datetime | None = None

    @model_validator(mode="after")
    def validate_snapshot(self) -> "PaperMarketSnapshotTestRequest":
        if self.ask < self.bid:
            raise ValueError("ask must be greater than or equal to bid")
        if self.timestamp is not None and self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return self


class PaperPnl(BaseModel):
    starting_equity: float
    equity: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    fees_paid: float = Field(default=0.0, ge=0)
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
