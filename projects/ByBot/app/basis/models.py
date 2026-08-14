from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

DAY_MS = 86_400_000

# Bybit VIP0. The perpetual-versus-future implementation pays futures fees on
# both legs; the spot-versus-future one pays 10 bps per side on the spot leg,
# which is what made cash-and-carry unviable earlier in this research.
FUTURES_TAKER_BPS = Decimal("5.5")
FUTURES_MAKER_BPS = Decimal("2.0")
SPOT_TAKER_BPS = Decimal("10.0")
SLIPPAGE_BPS = Decimal("2.5")


@dataclass(frozen=True)
class Quote:
    """One side of the trade as the book actually shows it."""

    symbol: str
    mid: float
    spread_bps: float
    depth_usd: float


@dataclass(frozen=True)
class BasisObservation:
    """One dated contract measured against its reference leg."""

    observed_at_ms: int
    base_coin: str
    future: Quote
    reference: Quote
    reference_kind: str  # "perp" or "spot"
    delivery_ms: int

    @property
    def days_to_delivery(self) -> float:
        return max((self.delivery_ms - self.observed_at_ms) / DAY_MS, 0.0)

    @property
    def basis_bps(self) -> float:
        if self.reference.mid <= 0:
            return 0.0
        return (self.future.mid / self.reference.mid - 1.0) * 10_000.0

    @property
    def annualised_bps(self) -> float:
        days = self.days_to_delivery
        return self.basis_bps * 365.0 / days if days > 0 else 0.0

    @property
    def capacity_usd(self) -> float:
        """The thinner book binds: both legs must fill for the trade to exist."""
        return min(self.future.depth_usd, self.reference.depth_usd)

    def round_trip_bps(self, *, maker: bool = False) -> float:
        """Enter and exit both legs, including the spreads actually quoted."""
        fee = float(FUTURES_MAKER_BPS if maker else FUTURES_TAKER_BPS)
        reference_fee = (
            float(SPOT_TAKER_BPS) if self.reference_kind == "spot" else fee
        )
        return (
            2 * fee
            + 2 * reference_fee
            + self.future.spread_bps
            + self.reference.spread_bps
            + float(SLIPPAGE_BPS)
        )

    def net_annualised_bps(self, *, maker: bool = False) -> float:
        days = self.days_to_delivery
        if days <= 0:
            return 0.0
        return (self.basis_bps - self.round_trip_bps(maker=maker)) * 365.0 / days

    @property
    def tradeable(self) -> bool:
        return self.capacity_usd > 0 and self.days_to_delivery > 1


@dataclass(frozen=True)
class CurveAlert:
    """A contract whose annualised basis has left its observed range."""

    symbol: str
    annualised_bps: float
    median_bps: float
    deviation_bps: float
    observations: int
    direction: str  # "rich" or "cheap"

    def describe(self) -> str:
        return (
            f"{self.symbol}: {self.annualised_bps:+.0f} bps/yr is {self.direction} "
            f"by {abs(self.deviation_bps):.0f} bps against a median of "
            f"{self.median_bps:+.0f} over {self.observations} observations"
        )
