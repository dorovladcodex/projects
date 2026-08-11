from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DataAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


class ExecutionScenario(StrEnum):
    TAKER_TAKER = "TAKER_TAKER"
    MAKER_TAKER = "MAKER_TAKER"
    TAKER_MAKER = "TAKER_MAKER"
    MAKER_MAKER = "MAKER_MAKER"
    MAKER_WITH_BOUNDED_TAKER_FALLBACK = "MAKER_WITH_BOUNDED_TAKER_FALLBACK"


class MarketLegSnapshot(BaseModel):
    """One public market leg. It contains observations, never order state."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=30)
    category: str = Field(pattern="^(spot|linear)$")
    source_timestamp: datetime
    received_at: datetime
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    mark_price: Decimal | None = Field(default=None, gt=0)
    index_price: Decimal | None = Field(default=None, gt=0)
    bid_depth_usdt: Decimal | None = Field(default=None, ge=0)
    ask_depth_usdt: Decimal | None = Field(default=None, ge=0)
    slippage_bps_by_notional: dict[str, Decimal | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_timing_and_book(self) -> "MarketLegSnapshot":
        if self.source_timestamp > self.received_at:
            raise ValueError("market source timestamp cannot be after receipt")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("best bid cannot exceed best ask")
        return self

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / Decimal("2")


class FundingPayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    rate: Decimal
    interval_hours: Decimal = Field(gt=0)
    source: str = Field(min_length=1)
    authoritative: bool = True


class CarryOpportunity(BaseModel):
    """Immutable-by-convention V5 carry opportunity observation."""

    model_config = ConfigDict(extra="forbid")

    opportunity_id: UUID
    timestamp: datetime
    symbol: str
    spot_symbol: str
    perp_symbol: str
    spot_source_timestamp: datetime | None = None
    perp_source_timestamp: datetime | None = None
    alignment_ms: Decimal | None = Field(default=None, ge=0)

    spot_bid: Decimal | None = Field(default=None, gt=0)
    spot_ask: Decimal | None = Field(default=None, gt=0)
    spot_mid: Decimal | None = Field(default=None, gt=0)
    perp_bid: Decimal | None = Field(default=None, gt=0)
    perp_ask: Decimal | None = Field(default=None, gt=0)
    perp_mid: Decimal | None = Field(default=None, gt=0)
    mark_price: Decimal | None = Field(default=None, gt=0)
    index_price: Decimal | None = Field(default=None, gt=0)
    basis_bps: Decimal | None = None

    current_funding_rate: Decimal | None = None
    predicted_funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None
    funding_interval_hours: Decimal | None = Field(default=None, gt=0)
    historical_funding: dict[str, Decimal | None] = Field(default_factory=dict)
    funding_persistence: dict[str, Decimal | None] = Field(default_factory=dict)

    spot_spread_bps: Decimal | None = Field(default=None, ge=0)
    perp_spread_bps: Decimal | None = Field(default=None, ge=0)
    spot_bid_depth_usdt: Decimal | None = Field(default=None, ge=0)
    spot_ask_depth_usdt: Decimal | None = Field(default=None, ge=0)
    perp_bid_depth_usdt: Decimal | None = Field(default=None, ge=0)
    perp_ask_depth_usdt: Decimal | None = Field(default=None, ge=0)
    spot_slippage_bps: dict[str, Decimal | None] = Field(default_factory=dict)
    perp_slippage_bps: dict[str, Decimal | None] = Field(default_factory=dict)

    account_fees_bps: dict[str, Decimal | None] = Field(default_factory=dict)
    expected_costs_bps: dict[str, Decimal | None] = Field(default_factory=dict)
    availability: dict[str, DataAvailability] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    source: str = "V5_CARRY_SHADOW"
    shadow_only: bool = True
    executed: bool = False

    @model_validator(mode="after")
    def validate_shadow_and_time(self) -> "CarryOpportunity":
        if not self.shadow_only or self.executed:
            raise ValueError("V5 carry opportunities are shadow-only and non-executing")
        for source_at in (self.spot_source_timestamp, self.perp_source_timestamp):
            if source_at is not None and source_at > self.timestamp:
                raise ValueError("opportunity cannot use future market data")
        return self


class CarryLabel(BaseModel):
    """Historical path label; UNKNOWN is mandatory when exact inputs are absent."""

    model_config = ConfigDict(extra="forbid")

    opportunity_id: UUID
    symbol: str
    horizon: str
    horizon_end: datetime | None = None
    coverage: DataAvailability
    funding_income: Decimal | None = None
    basis_change_pnl: Decimal | None = None
    spot_leg_pnl: Decimal | None = None
    perp_leg_pnl: Decimal | None = None
    hedged_gross_pnl: Decimal | None = None
    entry_cost: Decimal | None = None
    exit_cost: Decimal | None = None
    estimated_slippage: Decimal | None = None
    funding_received: Decimal | None = None
    funding_paid: Decimal | None = None
    net_carry_pnl: Decimal | None = None
    net_carry_bps: Decimal | None = None
    max_basis_adverse_excursion_bps: Decimal | None = None
    max_hedge_imbalance_bps: Decimal | None = None
    funding_sign_flip: bool | None = None
    time_to_break_even_seconds: Decimal | None = None
    blockers: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    first_fill_time: datetime | None = None

    @model_validator(mode="after")
    def validate_unknown_and_no_fill(self) -> "CarryLabel":
        if self.first_fill_time is not None:
            raise ValueError("research carry labels cannot fabricate fills")
        economic = (
            self.funding_income,
            self.spot_leg_pnl,
            self.perp_leg_pnl,
            self.hedged_gross_pnl,
            self.net_carry_pnl,
            self.net_carry_bps,
        )
        if self.coverage != DataAvailability.AVAILABLE and any(
            value is not None for value in economic
        ):
            raise ValueError("partial/unknown labels cannot contain completed economics")
        return self
