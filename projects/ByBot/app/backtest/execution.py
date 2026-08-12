from __future__ import annotations

from dataclasses import dataclass

from app.backtest.costs import Liquidity
from app.backtest.data import Bar


@dataclass(frozen=True)
class Fill:
    price: float
    liquidity: Liquidity


@dataclass
class FillStats:
    requested: int = 0
    filled: int = 0
    missed: int = 0
    missed_notional: float = 0.0

    @property
    def fill_rate(self) -> float:
        return self.filled / self.requested if self.requested else 0.0


class TakerFills:
    """Cross the spread at the next bar's open. Always fills."""

    liquidity = Liquidity.TAKER

    def __init__(self) -> None:
        self.stats = FillStats()

    def attempt(self, bar: Bar, reference: float | None, buying: bool) -> Fill | None:
        self.stats.requested += 1
        self.stats.filled += 1
        return Fill(price=bar.open, liquidity=Liquidity.TAKER)


class MakerFills:
    """Post-only at the previous close; fills only if the bar trades through.

    This is the honest version of "just use maker fees". A resting order does
    not fill on demand: a buy fills only when price comes down to it, and is
    missed when price gaps away. That asymmetry is adverse selection — the
    fills you get are disproportionately the ones you did not want — and it is
    the reason a cheaper fee schedule is not automatically cheaper trading.

    With hourly bars the test is whether the bar's range reached the resting
    price. That is a generous reading of a real queue, so results here are an
    upper bound on what maker execution can deliver, not a promise.
    """

    liquidity = Liquidity.MAKER

    def __init__(self) -> None:
        self.stats = FillStats()

    def attempt(self, bar: Bar, reference: float | None, buying: bool) -> Fill | None:
        self.stats.requested += 1
        if reference is None:
            self.stats.missed += 1
            return None

        touched = bar.low <= reference if buying else bar.high >= reference
        if not touched:
            self.stats.missed += 1
            return None

        self.stats.filled += 1
        return Fill(price=reference, liquidity=Liquidity.MAKER)


def build_fill_model(liquidity: Liquidity):
    return MakerFills() if liquidity is Liquidity.MAKER else TakerFills()
