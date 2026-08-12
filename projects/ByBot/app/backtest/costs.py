from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Liquidity(StrEnum):
    TAKER = "TAKER"
    MAKER = "MAKER"


@dataclass(frozen=True)
class CostModel:
    """Round-trip trading costs in basis points of notional.

    Defaults are Bybit VIP0. The perpetual numbers are corroborated by the
    project's own execution history: 253 Demo trades measured an 11.00 bps
    perpetual round trip, exactly 2 x 5.5 bps taker.

    Spot fees are deliberately not assumed to be cheaper than perpetual fees.
    At VIP0 Bybit charges 0.1% on spot either side, so a two-legged carry trade
    pays far more on the spot leg than on the perpetual one. Any carry result
    that ignores this is fiction.
    """

    perp_taker_bps: Decimal = Decimal("5.5")
    perp_maker_bps: Decimal = Decimal("2.0")
    spot_taker_bps: Decimal = Decimal("10.0")
    spot_maker_bps: Decimal = Decimal("10.0")
    slippage_bps: Decimal = Decimal("1.0")

    def entry_bps(self, venue: str, liquidity: Liquidity) -> Decimal:
        return self._fee(venue, liquidity) + self.slippage_bps

    def exit_bps(self, venue: str, liquidity: Liquidity) -> Decimal:
        return self._fee(venue, liquidity) + self.slippage_bps

    def round_trip_bps(self, venue: str, liquidity: Liquidity) -> Decimal:
        return self.entry_bps(venue, liquidity) + self.exit_bps(venue, liquidity)

    def carry_round_trip_bps(self, liquidity: Liquidity = Liquidity.TAKER) -> Decimal:
        """Both legs, opened and closed: the real hurdle for cash-and-carry."""
        return self.round_trip_bps("perp", liquidity) + self.round_trip_bps("spot", liquidity)

    def _fee(self, venue: str, liquidity: Liquidity) -> Decimal:
        if venue == "perp":
            return self.perp_taker_bps if liquidity is Liquidity.TAKER else self.perp_maker_bps
        if venue == "spot":
            return self.spot_taker_bps if liquidity is Liquidity.TAKER else self.spot_maker_bps
        raise ValueError(f"unknown venue: {venue}")

    def breakeven_days(
        self, funding_bps_per_day: Decimal, liquidity: Liquidity = Liquidity.TAKER
    ) -> Decimal | None:
        """How long a carry position must be held before costs are recovered."""
        if funding_bps_per_day <= 0:
            return None
        return self.carry_round_trip_bps(liquidity) / funding_bps_per_day


STRESS_MULTIPLIERS: tuple[Decimal, ...] = (Decimal("1.0"), Decimal("1.5"), Decimal("2.0"))


def stressed(model: CostModel, multiplier: Decimal) -> CostModel:
    """Scale every cost component, for sensitivity runs."""
    return CostModel(
        perp_taker_bps=model.perp_taker_bps * multiplier,
        perp_maker_bps=model.perp_maker_bps * multiplier,
        spot_taker_bps=model.spot_taker_bps * multiplier,
        spot_maker_bps=model.spot_maker_bps * multiplier,
        slippage_bps=model.slippage_bps * multiplier,
    )
