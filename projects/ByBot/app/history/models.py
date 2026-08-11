from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KlineInterval(StrEnum):
    """Bybit V5 kline intervals used by research backfills."""

    ONE_MINUTE = "1"
    FIVE_MINUTES = "5"
    FIFTEEN_MINUTES = "15"
    THIRTY_MINUTES = "30"
    ONE_HOUR = "60"
    FOUR_HOURS = "240"
    ONE_DAY = "D"

    @property
    def milliseconds(self) -> int:
        return _INTERVAL_MILLISECONDS[self]


_INTERVAL_MILLISECONDS: dict[KlineInterval, int] = {
    KlineInterval.ONE_MINUTE: 60_000,
    KlineInterval.FIVE_MINUTES: 300_000,
    KlineInterval.FIFTEEN_MINUTES: 900_000,
    KlineInterval.THIRTY_MINUTES: 1_800_000,
    KlineInterval.ONE_HOUR: 3_600_000,
    KlineInterval.FOUR_HOURS: 14_400_000,
    KlineInterval.ONE_DAY: 86_400_000,
}


class OpenInterestInterval(StrEnum):
    FIVE_MINUTES = "5min"
    FIFTEEN_MINUTES = "15min"
    THIRTY_MINUTES = "30min"
    ONE_HOUR = "1h"
    FOUR_HOURS = "4h"
    ONE_DAY = "1d"

    @property
    def milliseconds(self) -> int:
        return _OPEN_INTEREST_MILLISECONDS[self]


_OPEN_INTEREST_MILLISECONDS: dict[OpenInterestInterval, int] = {
    OpenInterestInterval.FIVE_MINUTES: 300_000,
    OpenInterestInterval.FIFTEEN_MINUTES: 900_000,
    OpenInterestInterval.THIRTY_MINUTES: 1_800_000,
    OpenInterestInterval.ONE_HOUR: 3_600_000,
    OpenInterestInterval.FOUR_HOURS: 14_400_000,
    OpenInterestInterval.ONE_DAY: 86_400_000,
}


def to_utc(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc)


class Kline(BaseModel):
    """One completed OHLCV bar. Prices stay Decimal to avoid float drift."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1)
    interval: KlineInterval
    start_ms: int = Field(gt=0)
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    turnover: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> "Kline":
        if self.high < self.low:
            raise ValueError(f"kline high < low for {self.symbol} at {self.start_ms}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"kline open outside range for {self.symbol} at {self.start_ms}")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"kline close outside range for {self.symbol} at {self.start_ms}")
        return self

    @property
    def start_at(self) -> datetime:
        return to_utc(self.start_ms)


class FundingRate(BaseModel):
    """One settled funding payment. The rate may legitimately be negative."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1)
    funding_rate: Decimal
    funding_time_ms: int = Field(gt=0)

    @property
    def funding_at(self) -> datetime:
        return to_utc(self.funding_time_ms)


class OpenInterest(BaseModel):
    """Open interest observed at one bucket boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1)
    interval: OpenInterestInterval
    timestamp_ms: int = Field(gt=0)
    open_interest: Decimal = Field(ge=0)

    @property
    def observed_at(self) -> datetime:
        return to_utc(self.timestamp_ms)
