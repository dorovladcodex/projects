from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import math
from random import Random
from typing import Iterable, Sequence

from app.v2.models import StrategySide, V2SignalCandidate


@dataclass(frozen=True)
class CalibrationObservation:
    strategy: str
    symbol: str
    regime: str
    net_return_bps: Decimal
    opened_at: datetime
    closed_at: datetime


@dataclass(frozen=True)
class CalibrationEstimate:
    ready: bool
    sample_count: int
    win_probability: Decimal | None
    win_probability_lower_bound: Decimal | None
    expected_net_edge_bps: Decimal | None
    status: str


class EmpiricalEdgeCalibrator:
    """Shrinkage estimator used as a shadow meta-label until enough outcomes exist."""

    def __init__(self, minimum_samples: int = 200) -> None:
        self.minimum_samples = minimum_samples
        self._observations: list[CalibrationObservation] = []

    def fit(self, observations: Iterable[CalibrationObservation]) -> None:
        self._observations = sorted(observations, key=lambda row: row.closed_at)

    def estimate(self, candidate: V2SignalCandidate) -> CalibrationEstimate:
        exact = [
            row for row in self._observations
            if row.strategy == candidate.strategy_name.value
            and row.symbol == candidate.symbol.value
            and row.regime == candidate.market_regime
        ]
        strategy_rows = [
            row for row in self._observations
            if row.strategy == candidate.strategy_name.value
        ]
        # Hierarchical shrinkage prevents a sparse symbol/regime bucket from
        # producing extreme confidence.
        rows = exact if len(exact) >= self.minimum_samples else strategy_rows
        if not rows:
            return CalibrationEstimate(False, 0, None, None, None, "UNCALIBRATED")
        wins = sum(row.net_return_bps > 0 for row in rows)
        count = len(rows)
        probability = Decimal(wins + 1) / Decimal(count + 2)  # Beta(1,1) posterior mean.
        lower = Decimal(str(_wilson_lower_bound(wins, count)))
        mean = sum((row.net_return_bps for row in rows), Decimal("0")) / Decimal(count)
        ready = count >= self.minimum_samples
        return CalibrationEstimate(
            ready=ready,
            sample_count=count,
            win_probability=probability,
            win_probability_lower_bound=lower,
            expected_net_edge_bps=mean,
            status="READY" if ready else "SHADOW_INSUFFICIENT_SAMPLES",
        )


def triple_barrier_label(
    *, entry_price: Decimal, side: StrategySide,
    future_prices: Sequence[tuple[datetime, Decimal]],
    stop_loss_pct: Decimal, take_profit_pct: Decimal,
    expires_at: datetime, round_trip_cost_bps: Decimal,
) -> Decimal:
    """Return realized net bps using the first chronological barrier hit."""
    direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
    last = entry_price
    for timestamp, price in sorted(future_prices, key=lambda row: row[0]):
        if timestamp > expires_at:
            break
        last = price
        gross_bps = direction * (price / entry_price - Decimal("1")) * Decimal("10000")
        if gross_bps <= -(stop_loss_pct * Decimal("100")):
            return gross_bps - round_trip_cost_bps
        if gross_bps >= take_profit_pct * Decimal("100"):
            return gross_bps - round_trip_cost_bps
    return direction * (last / entry_price - Decimal("1")) * Decimal("10000") - round_trip_cost_bps


def purged_walk_forward_splits(
    rows: Sequence[CalibrationObservation], *, folds: int = 5,
    embargo: timedelta = timedelta(hours=3),
) -> list[tuple[list[int], list[int]]]:
    """Chronological folds with overlapping labels purged and an embargo."""
    if folds < 2 or len(rows) < folds:
        return []
    ordered = sorted(enumerate(rows), key=lambda item: item[1].opened_at)
    fold_size = max(1, len(ordered) // folds)
    result: list[tuple[list[int], list[int]]] = []
    for fold in range(1, folds):
        test_rows = ordered[fold * fold_size : (fold + 1) * fold_size]
        if not test_rows:
            continue
        test_start = test_rows[0][1].opened_at
        train = [
            index for index, row in ordered[: fold * fold_size]
            if row.closed_at < test_start - embargo
        ]
        result.append((train, [index for index, _ in test_rows]))
    return result


def bootstrap_mean_confidence_interval(
    values: Sequence[Decimal], *, samples: int = 2000,
    seed: int = 7,
) -> tuple[Decimal | None, Decimal | None]:
    if not values:
        return None, None
    rng = Random(seed)
    means: list[Decimal] = []
    for _ in range(samples):
        draw = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(draw, Decimal("0")) / Decimal(len(draw)))
    means.sort()
    return means[int(samples * 0.025)], means[min(samples - 1, int(samples * 0.975))]


def cost_stress_expectancy(
    gross_returns_bps: Sequence[Decimal], base_cost_bps: Decimal,
) -> dict[str, Decimal | None]:
    if not gross_returns_bps:
        return {"1x": None, "2x": None, "3x": None}
    return {
        f"{multiple}x": sum(
            (value - base_cost_bps * Decimal(multiple) for value in gross_returns_bps),
            Decimal("0"),
        ) / Decimal(len(gross_returns_bps))
        for multiple in (1, 2, 3)
    }


def _wilson_lower_bound(wins: int, count: int, z: float = 1.96) -> float:
    if count <= 0:
        return 0.0
    p = wins / count
    denominator = 1 + z * z / count
    centre = p + z * z / (2 * count)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * count)) / count)
    return max(0.0, (centre - margin) / denominator)
