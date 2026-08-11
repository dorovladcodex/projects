from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CoverageState(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    STALE = "STALE"


class BookLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: Decimal = Field(gt=0)
    quantity: Decimal = Field(ge=0)


class LegSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(pattern="^(spot|linear)$")
    symbol: str
    exchange_timestamp: datetime
    local_receive_timestamp: datetime
    best_bid: Decimal = Field(gt=0)
    best_ask: Decimal = Field(gt=0)
    mid: Decimal = Field(gt=0)
    spread_bps: Decimal = Field(ge=0)
    bids: list[BookLevel]
    asks: list[BookLevel]
    depth_bps_usdt: dict[str, dict[str, Decimal]]
    recent_trade_price: Decimal | None = Field(default=None, gt=0)
    recent_trade_volume: Decimal | None = Field(default=None, ge=0)
    recent_trade_timestamp: datetime | None = None
    mark_price: Decimal | None = Field(default=None, gt=0)
    index_price: Decimal | None = Field(default=None, gt=0)
    current_funding_rate: Decimal | None = None
    funding_timestamp: datetime | None = None
    predicted_funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None
    funding_interval_minutes: int | None = Field(default=None, gt=0)
    premium_index: Decimal | None = None
    open_interest: Decimal | None = Field(default=None, ge=0)
    open_interest_timestamp: datetime | None = None
    open_interest_change_pct: Decimal | None = None
    volatility_5m_bps: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_book(self) -> "LegSnapshot":
        if self.best_bid >= self.best_ask:
            raise ValueError("leg best bid must remain below best ask")
        expected = (self.best_bid + self.best_ask) / Decimal("2")
        if self.mid != expected:
            raise ValueError("leg mid must equal the top-of-book midpoint")
        # Exchange and local clocks are independent. A positive server clock
        # offset can legitimately place the exact exchange timestamp after the
        # local receipt timestamp; the synchronized record preserves both and
        # carries the measured clock offset explicitly.
        return self


class SynchronizedSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_id: str
    symbol: str
    exchange_timestamp: datetime
    local_receive_timestamp: datetime
    snapshot_completed_at: datetime
    spot_age_ms: Decimal = Field(ge=0)
    perp_age_ms: Decimal = Field(ge=0)
    funding_age_ms: Decimal | None = Field(default=None, ge=0)
    synchronization_gap_ms: Decimal = Field(ge=0)
    clock_offset_ms: Decimal | None = None
    spot: LegSnapshot | None = None
    perpetual: LegSnapshot | None = None
    perp_mid_vs_spot_mid_bps: Decimal | None = None
    mark_vs_spot_bps: Decimal | None = None
    mark_vs_index_bps: Decimal | None = None
    complete: bool
    quality_reasons: list[str] = Field(default_factory=list)
    availability: dict[str, CoverageState] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_chronology(self) -> "SynchronizedSnapshot":
        if self.local_receive_timestamp > self.snapshot_completed_at:
            raise ValueError("snapshot completion cannot precede local receipt")
        if self.complete and self.quality_reasons:
            raise ValueError("complete snapshots cannot retain quality rejection reasons")
        return self


class TakerCostEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cost_id: str
    capture_id: str
    symbol: str
    venue_leg: str = Field(pattern="^(spot|perpetual)$")
    side: str = Field(pattern="^(BUY|SELL)$")
    notional_usdt: Decimal = Field(gt=0)
    sufficient_depth: bool
    filled_notional_usdt: Decimal = Field(ge=0)
    vwap: Decimal | None = Field(default=None, gt=0)
    slippage_bps: Decimal | None = None
    spread_cross_bps: Decimal | None = None
    fee_bps: Decimal | None = Field(default=None, ge=0)
    estimated_effective_cost_bps: Decimal | None = None
    blockers: list[str] = Field(default_factory=list)


class CarryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str
    capture_id: str
    symbol: str
    timestamp: datetime
    classification: str
    structure: str | None = None
    funding_rate: Decimal | None = None
    basis_bps: Decimal | None = None
    notional_costs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    canonical_opportunity: dict[str, Any]
    blockers: list[str] = Field(default_factory=list)
    exchange_mutation: bool = False


class FundingEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    symbol: str
    funding_timestamp: datetime
    observed_at: datetime
    funding_rate: Decimal
    mark_price: Decimal | None = Field(default=None, gt=0)
    index_price: Decimal | None = Field(default=None, gt=0)
    spot_perp_basis_bps: Decimal | None = None
    open_interest: Decimal | None = Field(default=None, ge=0)
    volatility_context_bps: Decimal | None = Field(default=None, ge=0)
    context_coverage: CoverageState


class HypotheticalQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: str
    capture_id: str
    symbol: str
    venue_leg: str = Field(pattern="^(spot|perpetual)$")
    side: str = Field(pattern="^(BUY|SELL)$")
    quote_price: Decimal = Field(gt=0)
    quote_time: datetime
    best_bid: Decimal = Field(gt=0)
    best_ask: Decimal = Field(gt=0)
    spread_bps: Decimal = Field(ge=0)
    terminology: str = "HYPOTHETICAL_TOUCH"
    submitted: bool = False

    @model_validator(mode="after")
    def prohibit_fill_language_and_submission(self) -> "HypotheticalQuote":
        if self.terminology != "HYPOTHETICAL_TOUCH":
            raise ValueError("maker telemetry must use HYPOTHETICAL_TOUCH terminology")
        if self.submitted:
            raise ValueError("hypothetical quotes cannot be submitted")
        return self


class HypotheticalTouchOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_id: str
    horizon_seconds: int = Field(gt=0)
    evaluated_at: datetime
    would_touch: bool
    estimated_time_to_touch_seconds: Decimal | None = Field(default=None, ge=0)
    touch_time: datetime | None = None
    markout_bps: Decimal | None = None
    complete: bool
    terminology: str = "HYPOTHETICAL_TOUCH"
    actual_fill_claimed: bool = False

    @model_validator(mode="after")
    def prohibit_actual_fill(self) -> "HypotheticalTouchOutcome":
        if self.terminology != "HYPOTHETICAL_TOUCH" or self.actual_fill_claimed:
            raise ValueError("touch telemetry cannot claim an actual fill")
        if not self.would_touch and self.markout_bps is not None:
            raise ValueError("untouched quote cannot have a post-touch markout")
        return self
