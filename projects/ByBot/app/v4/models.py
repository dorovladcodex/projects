from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FeatureAvailability(StrEnum):
    PRE_ENTRY_AVAILABLE = "PRE_ENTRY_AVAILABLE"
    POST_ENTRY_ONLY = "POST_ENTRY_ONLY"
    UNKNOWN = "UNKNOWN"


class V4Decision(StrEnum):
    SHADOW_TRADE = "SHADOW_TRADE"
    NO_TRADE = "NO_TRADE"
    EXECUTED_OBSERVED = "EXECUTED_OBSERVED"


class V4RejectionReason(StrEnum):
    NO_VOLATILITY_EXPANSION = "NO_VOLATILITY_EXPANSION"
    BREAKOUT_NOT_CONFIRMED = "BREAKOUT_NOT_CONFIRMED"
    VOLUME_CONFIRMATION_FAILED = "VOLUME_CONFIRMATION_FAILED"
    ORDER_FLOW_CONTRADICTS = "ORDER_FLOW_CONTRADICTS"
    REGIME_CONTRADICTS = "REGIME_CONTRADICTS"
    OI_CONFIRMATION_FAILED = "OI_CONFIRMATION_FAILED"
    LIQUIDITY_INSUFFICIENT = "LIQUIDITY_INSUFFICIENT"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    DATA_STALE = "DATA_STALE"
    ALREADY_MOVED_TOO_FAR = "ALREADY_MOVED_TOO_FAR"
    EXPECTED_MOVE_BELOW_COST = "EXPECTED_MOVE_BELOW_COST"
    META_MODEL_NO_TRADE = "META_MODEL_NO_TRADE"
    UNCERTAINTY_TOO_HIGH = "UNCERTAINTY_TOO_HIGH"
    FEATURES_INCOMPLETE = "FEATURES_INCOMPLETE"


class FeatureTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    source_timestamp: datetime | None = None
    local_receive_timestamp: datetime | None = None
    age_ms: Decimal | None = Field(default=None, ge=0)
    freshness_limit_ms: Decimal | None = Field(default=None, ge=0)
    availability: FeatureAvailability


class V4Opportunity(BaseModel):
    """Canonical, immutable-by-convention opportunity-tape row."""

    model_config = ConfigDict(extra="forbid")

    opportunity_id: UUID
    cycle_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=20)
    side: str = Field(pattern="^(BUY|SELL)$")
    source: str
    strategy_family: str = "VOLATILITY_EXPANSION_WITH_POSITIONING_CONFIRMATION"
    candidate_type: str = "V4_VOLATILITY_EXPANSION"

    event_time: datetime | None = None
    first_seen_time: datetime
    feature_snapshot_time: datetime
    candidate_time: datetime
    decision_time: datetime
    signal_time: datetime | None = None
    order_possible_time: datetime | None = None
    order_submit_time: datetime | None = None
    order_ack_time: datetime | None = None
    first_fill_time: datetime | None = None

    entry_reference_price: Decimal = Field(gt=0)
    features: dict[str, Any]
    feature_timing: dict[str, FeatureTiming]
    availability: dict[str, FeatureAvailability]
    decision: V4Decision
    rejection_reasons: list[V4RejectionReason] = Field(default_factory=list)
    candidate_layers: dict[str, bool] = Field(default_factory=dict)
    shadow_only: bool = True
    executed: bool = False

    @model_validator(mode="after")
    def validate_temporal_and_shadow_invariants(self) -> "V4Opportunity":
        ordered = (
            self.first_seen_time,
            self.feature_snapshot_time,
            self.candidate_time,
            self.decision_time,
        )
        if list(ordered) != sorted(ordered):
            raise ValueError("V4 opportunity timestamps must be chronological")
        if self.event_time is not None and self.event_time > self.first_seen_time:
            raise ValueError("event_time cannot occur after first_seen_time")
        if self.shadow_only and self.executed:
            raise ValueError("a shadow-only opportunity cannot be executed")
        if self.executed and self.decision != V4Decision.EXECUTED_OBSERVED:
            raise ValueError("executed observations require EXECUTED_OBSERVED decision")
        if not self.executed and self.decision == V4Decision.EXECUTED_OBSERVED:
            raise ValueError("EXECUTED_OBSERVED requires executed=true")
        if self.shadow_only and any(
            value is not None
            for value in (
                self.order_submit_time,
                self.order_ack_time,
                self.first_fill_time,
            )
        ):
            raise ValueError("shadow opportunities cannot contain exchange timestamps")
        return self


class V4ForwardLabel(BaseModel):
    """Market-path label.  It is never a hypothetical order fill."""

    model_config = ConfigDict(extra="forbid")

    opportunity_id: UUID
    symbol: str
    side: str = Field(pattern="^(BUY|SELL)$")
    decision_time: datetime
    label_generated_at: datetime
    maximum_horizon_seconds: int = Field(default=900, gt=0)
    path_source: str = "v2_market_feature_snapshots"
    observation_count: int = Field(ge=0)
    labels: dict[str, Any]
    cost_components_bps: dict[str, Decimal]
    complete: bool = False
    first_fill_time: datetime | None = None

    @model_validator(mode="after")
    def forbid_fabricated_fill(self) -> "V4ForwardLabel":
        if self.first_fill_time is not None:
            raise ValueError("market-path labels cannot fabricate a first fill")
        return self
