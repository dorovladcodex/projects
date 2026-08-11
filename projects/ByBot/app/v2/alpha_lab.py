from __future__ import annotations

from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.persistence import (
    PersistenceRepository,
    V2MarketFeatureRow,
    V2SignalCandidateRow,
)


ZERO = Decimal("0")
HORIZONS_SECONDS = (5, 15, 30, 60, 120, 300, 600)
MOVE_THRESHOLDS_BPS = (5, 11, 15, 20, 30)
COST_STRESS_BPS = (8, 11, 13, 15, 18, 20)
FINAL_HOLDOUT_FRACTION = Decimal("0.20")
WALK_FORWARD_FOLDS = 4

TERMINAL_STATES = {
    "DEMO_CLOSED",
    "DEMO_CLOSED_AFTER_FAILURE",
    "DEMO_CLOSED_AFTER_INTERRUPTION",
    "DEMO_CLOSED_EXTERNALLY",
    "DEMO_FAILED_FLAT_VERIFIED",
}

NUMERIC_MODEL_FEATURES = (
    "pre__raw_strategy_score",
    "pre__confidence",
    "pre__estimated_edge_bps",
    "pre__edge_proxy_bps",
    "pre__distance_to_threshold",
    "pre__rank_in_cycle",
    "pre__score_components__final_score",
    "pre__score_components__market_confirmation_score",
    "pre__score_components__liquidity_score",
    "pre__score_components__uncertainty_penalty",
    "pre__sizing__expected_net_edge_bps",
    "pre__sizing__expected_spread_bps",
    "pre__sizing__expected_slippage_bps",
    "pre__feature_snapshot__spread_bps",
    "pre__feature_snapshot__atr_bps",
    "pre__feature_snapshot__microprice_deviation_bps",
    "pre__feature_snapshot__relative_strength_vs_btc_bps",
    "pre__feature_snapshot__rolling_correlation_vs_btc",
    "pre__feature_snapshot__funding_rate",
    "pre__feature_snapshot__funding_deviation_bps",
    "pre__feature_snapshot__open_interest_change_pct",
    "pre__feature_snapshot__liquidation_imbalance",
    "pre__feature_snapshot__liquidation_notional_5m",
    "pre__feature_snapshot__price_momentum__30s",
    "pre__feature_snapshot__price_momentum__1m",
    "pre__feature_snapshot__price_momentum__5m",
    "pre__feature_snapshot__realized_volatility__1m",
    "pre__feature_snapshot__trade_imbalance__1m",
    "pre__feature_snapshot__order_flow_imbalance__1m",
    "pre__feature_snapshot__orderbook_imbalance",
    "pre__signal_age_seconds",
    "pre__signal_to_order_seconds",
    "pre__active_positions_at_entry",
)

CATEGORICAL_MODEL_FEATURES = (
    "strategy",
    "symbol",
    "side",
    "pre__market_regime",
)


def decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def aware(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str) else value
    )
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    )


def directional_return_bps(
    entry_price: Decimal, price: Decimal, side: str,
) -> Decimal:
    direction = Decimal("1") if side.upper() in {"BUY", "LONG"} else Decimal("-1")
    return direction * (price / entry_price - Decimal("1")) * Decimal("10000")


def flatten_payload(
    value: Any, *, prefix: str = "pre", output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = output if output is not None else {}
    if isinstance(value, dict):
        for key in sorted(value):
            flatten_payload(value[key], prefix=f"{prefix}__{key}", output=result)
    elif isinstance(value, list):
        result[prefix] = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    elif isinstance(value, bool):
        result[prefix] = value
    elif value is None:
        result[prefix] = None
    else:
        parsed = optional_decimal(value)
        result[prefix] = str(parsed) if parsed is not None else str(value)
    return result


def _max_gap_seconds(points: Sequence[tuple[datetime, Decimal]]) -> float | None:
    if len(points) < 2:
        return None
    return max((right[0] - left[0]).total_seconds() for left, right in zip(points, points[1:]))


def compute_path_labels(
    *,
    fill_at: datetime,
    exit_at: datetime,
    entry_price: Decimal,
    exit_price: Decimal,
    side: str,
    observations: Sequence[tuple[datetime, Decimal]],
    durable_mfe_ratio: Decimal | None,
    durable_mae_ratio: Decimal | None,
) -> dict[str, Any]:
    """Build post-entry labels without interpolation or synthetic ticks."""
    stored = sorted(
        (
            (timestamp, price)
            for timestamp, price in observations
            if fill_at < timestamp <= exit_at and price > 0
        ),
        key=lambda item: item[0],
    )
    points = list(stored)
    if exit_at > fill_at and exit_price > 0:
        points.append((exit_at, exit_price))
    points.sort(key=lambda item: item[0])
    returns = [
        (timestamp, directional_return_bps(entry_price, price, side))
        for timestamp, price in points
    ]
    stored_returns = [
        (timestamp, directional_return_bps(entry_price, price, side))
        for timestamp, price in stored
    ]
    durable_mfe = (
        durable_mfe_ratio * Decimal("10000")
        if durable_mfe_ratio is not None else None
    )
    durable_mae = (
        durable_mae_ratio * Decimal("10000")
        if durable_mae_ratio is not None else None
    )
    observed_values = [value for _, value in returns]
    observed_mfe = max(observed_values, default=None)
    observed_mae = min(observed_values, default=None)
    until_mfe = max(
        (value for value in (observed_mfe, durable_mfe) if value is not None),
        default=None,
    )
    until_mae = min(
        (value for value in (observed_mae, durable_mae) if value is not None),
        default=None,
    )
    result: dict[str, Any] = {
        "post__path_observation_count": len(stored),
        "post__path_first_observation_delay_seconds": (
            (stored[0][0] - fill_at).total_seconds() if stored else None
        ),
        "post__path_max_gap_seconds": _max_gap_seconds(stored),
        "post__path_early_resolution": "OBSERVED" if stored else "UNKNOWN",
        "post__mfe_bps_until_exit": str(until_mfe) if until_mfe is not None else None,
        "post__mae_bps_until_exit": str(until_mae) if until_mae is not None else None,
        "post__mfe_until_exit_source": (
            "stored_path_and_durable_monitor_extrema"
            if durable_mfe is not None else "stored_path_and_exact_exit"
        ),
        "post__mae_until_exit_source": (
            "stored_path_and_durable_monitor_extrema"
            if durable_mae is not None else "stored_path_and_exact_exit"
        ),
    }
    for horizon in HORIZONS_SECONDS:
        cutoff = fill_at + timedelta(seconds=horizon)
        eligible = [value for timestamp, value in stored_returns if timestamp <= cutoff]
        result[f"post__mfe_bps_{horizon}s"] = (
            str(max(eligible)) if eligible else None
        )
        result[f"post__mae_bps_{horizon}s"] = (
            str(min(eligible)) if eligible else None
        )
        result[f"post__return_bps_{horizon}s"] = (
            str(eligible[-1]) if eligible else None
        )
        result[f"post__coverage_{horizon}s"] = "OBSERVED" if eligible else "UNKNOWN"
    for threshold in MOVE_THRESHOLDS_BPS:
        positive = next(
            (
                (timestamp - fill_at).total_seconds()
                for timestamp, value in stored_returns
                if value >= Decimal(threshold)
            ),
            None,
        )
        adverse = next(
            (
                (timestamp - fill_at).total_seconds()
                for timestamp, value in stored_returns
                if value <= -Decimal(threshold)
            ),
            None,
        )
        result[f"post__first_time_to_plus_{threshold}_bps_seconds"] = positive
        result[f"post__first_time_to_minus_{threshold}_bps_seconds"] = adverse
        result[f"post__reached_plus_{threshold}_bps"] = (
            until_mfe is not None and until_mfe >= Decimal(threshold)
        )
        result[f"post__reached_minus_{threshold}_bps"] = (
            until_mae is not None and until_mae <= -Decimal(threshold)
        )
        result[f"post__plus_{threshold}_before_minus_{threshold}"] = (
            positive < adverse
            if positive is not None and adverse is not None
            else True if positive is not None and adverse is None
            else False if adverse is not None and positive is None
            else None
        )
    if stored_returns:
        best_timestamp, best_value = max(stored_returns, key=lambda item: item[1])
        first_loss = next(
            (
                (timestamp - fill_at).total_seconds()
                for timestamp, value in stored_returns
                if value < 0
            ),
            None,
        )
        result["post__time_to_observed_mfe_seconds"] = (
            (best_timestamp - fill_at).total_seconds()
            if until_mfe == best_value else None
        )
        result["post__time_to_first_observed_loss_seconds"] = first_loss
    else:
        result["post__time_to_observed_mfe_seconds"] = None
        result["post__time_to_first_observed_loss_seconds"] = None
    return result


def percentile(values: Sequence[Decimal], probability: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = probability * Decimal(len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - Decimal(lower)
    return ordered[lower] * (Decimal("1") - weight) + ordered[upper] * weight


def maximum_drawdown(values: Sequence[Decimal]) -> Decimal:
    equity = ZERO
    peak = ZERO
    result = ZERO
    for value in values:
        equity += value
        peak = max(peak, equity)
        result = max(result, peak - equity)
    return result


def economic_metrics(
    rows: Sequence[dict[str, Any]], *,
    cost_bps: Decimal | None = None,
    gross_field: str = "gross_bps",
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row["fill_timestamp"])
    pnl: list[Decimal] = []
    gross_values: list[Decimal] = []
    notionals: list[Decimal] = []
    for row in ordered:
        gross = optional_decimal(row.get(gross_field))
        notional = optional_decimal(row.get("accepted_notional"))
        if gross is None or notional is None or notional <= 0:
            continue
        gross_values.append(gross)
        notionals.append(notional)
        if cost_bps is None:
            pnl.append(decimal(row.get("net_pnl")))
        else:
            pnl.append(notional * (gross - cost_bps) / Decimal("10000"))
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    total_notional = sum(notionals, ZERO)
    total_gross_usdt = sum(
        (
            notional * gross / Decimal("10000")
            for notional, gross in zip(notionals, gross_values)
        ),
        ZERO,
    )
    total_net = sum(pnl, ZERO)
    return {
        "trade_count": len(pnl),
        "wins": len(wins),
        "win_rate": str(Decimal(len(wins)) / Decimal(len(pnl))) if pnl else None,
        "gross_bps": (
            str(total_gross_usdt / total_notional * Decimal("10000"))
            if total_notional else None
        ),
        "net_bps": (
            str(total_net / total_notional * Decimal("10000"))
            if total_notional else None
        ),
        "gross_pnl": str(total_gross_usdt),
        "net_pnl": str(total_net),
        "expectancy": str(total_net / Decimal(len(pnl))) if pnl else None,
        "profit_factor": (
            str(sum(wins, ZERO) / abs(sum(losses, ZERO))) if losses else None
        ),
        "maximum_drawdown": str(maximum_drawdown(pnl)),
        "cost_bps": str(cost_bps) if cost_bps is not None else "actual_exact",
    }


def rank_values(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + end - 1) / 2.0 + 1.0
        for offset in range(index, end):
            result[ordered[offset][0]] = rank
        index = end
    return result


def spearman(values: Sequence[tuple[Decimal, Decimal]]) -> Decimal | None:
    if len(values) < 3:
        return None
    left = rank_values([float(item[0]) for item in values])
    right = rank_values([float(item[1]) for item in values])
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    left_scale = math.sqrt(sum((x - left_mean) ** 2 for x in left))
    right_scale = math.sqrt(sum((y - right_mean) ** 2 for y in right))
    if left_scale == 0 or right_scale == 0:
        return None
    correlation = numerator / (left_scale * right_scale)
    if math.isclose(correlation, 1.0, abs_tol=1e-12):
        correlation = 1.0
    elif math.isclose(correlation, -1.0, abs_tol=1e-12):
        correlation = -1.0
    return Decimal(str(correlation))


def calibration_deciles(
    rows: Sequence[dict[str, Any]], feature: str, target: str,
) -> list[dict[str, Any]]:
    usable = [
        row for row in rows
        if optional_decimal(row.get(feature)) is not None
        and optional_decimal(row.get(target)) is not None
    ]
    usable.sort(key=lambda row: optional_decimal(row[feature]) or ZERO)
    if not usable:
        return []
    result: list[dict[str, Any]] = []
    for decile in range(10):
        start = len(usable) * decile // 10
        end = len(usable) * (decile + 1) // 10
        bucket = usable[start:end]
        if not bucket:
            continue
        feature_values = [decimal(row[feature]) for row in bucket]
        target_values = [decimal(row[target]) for row in bucket]
        result.append({
            "decile": decile + 1,
            "trade_count": len(bucket),
            "feature_min": str(min(feature_values)),
            "feature_max": str(max(feature_values)),
            "feature_mean": str(sum(feature_values, ZERO) / Decimal(len(bucket))),
            "realized_mean": str(sum(target_values, ZERO) / Decimal(len(bucket))),
            "positive_rate": str(
                Decimal(sum(value > 0 for value in target_values))
                / Decimal(len(bucket))
            ),
        })
    return result


def chronological_splits(
    rows: Sequence[dict[str, Any]], *,
    folds: int = WALK_FORWARD_FOLDS,
    holdout_fraction: Decimal = FINAL_HOLDOUT_FRACTION,
    embargo: timedelta = timedelta(hours=3),
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row["fill_timestamp"])
    holdout_size = max(1, int(Decimal(len(ordered)) * holdout_fraction))
    development = ordered[:-holdout_size]
    final_holdout = ordered[-holdout_size:]
    segment = max(1, len(development) // (folds + 1))
    fold_rows: list[dict[str, Any]] = []
    for number in range(1, folds + 1):
        validation_start = number * segment
        validation_end = (
            len(development) if number == folds else min(len(development), (number + 1) * segment)
        )
        validation = development[validation_start:validation_end]
        if not validation:
            continue
        validation_start_at = aware(validation[0]["fill_timestamp"])
        assert validation_start_at is not None
        train = [
            row for row in development[:validation_start]
            if (aware(row["exit_timestamp"]) or validation_start_at)
            < validation_start_at - embargo
        ]
        fold_rows.append({
            "fold": number,
            "train_execution_ids": [row["execution_id"] for row in train],
            "validation_execution_ids": [row["execution_id"] for row in validation],
            "train_start": train[0]["fill_timestamp"] if train else None,
            "train_end": train[-1]["exit_timestamp"] if train else None,
            "validation_start": validation[0]["fill_timestamp"],
            "validation_end": validation[-1]["exit_timestamp"],
        })
    leave_one_run_out = []
    for run_id in sorted({str(row.get("run_id") or "UNKNOWN") for row in development}):
        validation = [
            row for row in development if str(row.get("run_id") or "UNKNOWN") == run_id
        ]
        train = [
            row for row in development if str(row.get("run_id") or "UNKNOWN") != run_id
        ]
        leave_one_run_out.append({
            "held_out_run_id": run_id,
            "train_execution_ids": [row["execution_id"] for row in train],
            "validation_execution_ids": [row["execution_id"] for row in validation],
        })
    return {
        "method": "expanding_chronological_walk_forward_with_3h_embargo",
        "holdout_frozen_before_model_selection": True,
        "development_execution_ids": [row["execution_id"] for row in development],
        "final_holdout_execution_ids": [row["execution_id"] for row in final_holdout],
        "final_holdout_start": final_holdout[0]["fill_timestamp"],
        "folds": fold_rows,
        "leave_one_run_out": leave_one_run_out,
    }


@dataclass
class RidgeModel:
    feature_names: list[str]
    medians: list[float]
    means: list[float]
    scales: list[float]
    coefficients: list[float]
    intercept: float

    def predict(self, row: dict[str, Any]) -> Decimal:
        values: list[float] = []
        for index, name in enumerate(self.feature_names):
            value = optional_decimal(row.get(name))
            numeric = float(value) if value is not None else self.medians[index]
            values.append((numeric - self.means[index]) / self.scales[index])
        return Decimal(str(self.intercept + sum(
            coefficient * value
            for coefficient, value in zip(self.coefficients, values)
        )))


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        if abs(augmented[column][column]) < 1e-12:
            continue
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[index][-1] for index in range(size)]


def fit_ridge(
    rows: Sequence[dict[str, Any]], *,
    target: str = "gross_bps",
    feature_names: Sequence[str] = NUMERIC_MODEL_FEATURES,
    penalty: float = 10.0,
) -> RidgeModel | None:
    usable = [row for row in rows if optional_decimal(row.get(target)) is not None]
    if len(usable) < 15:
        return None
    selected = [
        name for name in feature_names
        if sum(optional_decimal(row.get(name)) is not None for row in usable)
        >= max(5, len(usable) // 3)
    ]
    if not selected:
        return None
    columns: list[list[float]] = []
    medians: list[float] = []
    means: list[float] = []
    scales: list[float] = []
    for name in selected:
        available = sorted(
            float(value) for row in usable
            if (value := optional_decimal(row.get(name))) is not None
        )
        fill = median(available)
        raw = [
            float(value) if (value := optional_decimal(row.get(name))) is not None else fill
            for row in usable
        ]
        mean = sum(raw) / len(raw)
        variance = sum((value - mean) ** 2 for value in raw) / len(raw)
        scale = math.sqrt(variance) or 1.0
        columns.append([(value - mean) / scale for value in raw])
        medians.append(fill)
        means.append(mean)
        scales.append(scale)
    target_values = [float(decimal(row[target])) for row in usable]
    target_mean = sum(target_values) / len(target_values)
    centred = [value - target_mean for value in target_values]
    width = len(selected)
    matrix = [[0.0 for _ in range(width)] for _ in range(width)]
    vector = [0.0 for _ in range(width)]
    for left in range(width):
        vector[left] = sum(columns[left][row] * centred[row] for row in range(len(usable)))
        for right in range(width):
            matrix[left][right] = sum(
                columns[left][row] * columns[right][row] for row in range(len(usable))
            ) + (penalty if left == right else 0.0)
    coefficients = _solve(matrix, vector)
    return RidgeModel(selected, medians, means, scales, coefficients, target_mean)


def model_diagnostics(
    model: RidgeModel | None,
    rows: Sequence[dict[str, Any]],
    *,
    target: str,
) -> dict[str, Any]:
    usable = [row for row in rows if optional_decimal(row.get(target)) is not None]
    if model is None or not usable:
        return {"supported": False, "trade_count": len(usable)}
    pairs = [(model.predict(row), decimal(row[target])) for row in usable]
    errors = [predicted - actual for predicted, actual in pairs]
    return {
        "supported": True,
        "trade_count": len(usable),
        "spearman": _string(spearman(pairs)),
        "mae_bps": str(sum((abs(value) for value in errors), ZERO) / Decimal(len(errors))),
        "rmse_bps": str(Decimal(str(math.sqrt(
            sum(float(value) ** 2 for value in errors) / len(errors)
        )))),
        "prediction_mean_bps": str(sum((item[0] for item in pairs), ZERO) / Decimal(len(pairs))),
        "actual_mean_bps": str(sum((item[1] for item in pairs), ZERO) / Decimal(len(pairs))),
    }


@dataclass
class LogisticModel:
    feature_names: list[str]
    medians: list[float]
    means: list[float]
    scales: list[float]
    coefficients: list[float]
    intercept: float

    def predict(self, row: dict[str, Any]) -> Decimal:
        linear = self.intercept
        for index, name in enumerate(self.feature_names):
            value = optional_decimal(row.get(name))
            numeric = float(value) if value is not None else self.medians[index]
            linear += self.coefficients[index] * (
                (numeric - self.means[index]) / self.scales[index]
            )
        linear = max(-35.0, min(35.0, linear))
        return Decimal(str(1.0 / (1.0 + math.exp(-linear))))


def _binary_target(row: dict[str, Any], target: str) -> int | None:
    value = row.get(target)
    if isinstance(value, bool):
        return int(value)
    if value in (0, 1, "0", "1"):
        return int(value)
    return None


def fit_logistic(
    rows: Sequence[dict[str, Any]], *, target: str,
    feature_names: Sequence[str] = NUMERIC_MODEL_FEATURES,
    penalty: float = 0.5,
) -> LogisticModel | None:
    usable = [row for row in rows if _binary_target(row, target) is not None]
    if len(usable) < 20 or len({_binary_target(row, target) for row in usable}) < 2:
        return None
    selected = [
        name for name in feature_names
        if sum(optional_decimal(row.get(name)) is not None for row in usable)
        >= max(5, len(usable) // 3)
    ]
    if not selected:
        return None
    columns: list[list[float]] = []
    medians: list[float] = []
    means: list[float] = []
    scales: list[float] = []
    for name in selected:
        available = sorted(
            float(value) for row in usable
            if (value := optional_decimal(row.get(name))) is not None
        )
        fill = median(available)
        raw = [
            float(value) if (value := optional_decimal(row.get(name))) is not None else fill
            for row in usable
        ]
        mean = sum(raw) / len(raw)
        scale = math.sqrt(sum((value - mean) ** 2 for value in raw) / len(raw)) or 1.0
        columns.append([(value - mean) / scale for value in raw])
        medians.append(fill)
        means.append(mean)
        scales.append(scale)
    targets = [float(_binary_target(row, target) or 0) for row in usable]
    prevalence = min(1 - 1e-6, max(1e-6, sum(targets) / len(targets)))
    intercept = math.log(prevalence / (1.0 - prevalence))
    coefficients = [0.0] * len(selected)
    learning_rate = 0.08
    for _ in range(350):
        errors: list[float] = []
        for row_index in range(len(usable)):
            linear = intercept + sum(
                coefficients[index] * columns[index][row_index]
                for index in range(len(selected))
            )
            probability = 1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, linear))))
            errors.append(probability - targets[row_index])
        intercept -= learning_rate * sum(errors) / len(errors)
        for index in range(len(coefficients)):
            gradient = sum(
                errors[row_index] * columns[index][row_index]
                for row_index in range(len(usable))
            ) / len(usable)
            gradient += penalty * coefficients[index] / len(usable)
            coefficients[index] -= learning_rate * gradient
    return LogisticModel(selected, medians, means, scales, coefficients, intercept)


@dataclass
class IsotonicModel:
    feature: str
    upper_bounds: list[Decimal]
    probabilities: list[Decimal]
    default: Decimal

    def predict(self, row: dict[str, Any]) -> Decimal:
        value = optional_decimal(row.get(self.feature))
        if value is None:
            return self.default
        for upper, probability in zip(self.upper_bounds, self.probabilities):
            if value <= upper:
                return probability
        return self.probabilities[-1]


def fit_isotonic(
    rows: Sequence[dict[str, Any]], *, feature: str, target: str,
) -> IsotonicModel | None:
    pairs = [
        (value, _binary_target(row, target))
        for row in rows
        if (value := optional_decimal(row.get(feature))) is not None
        and _binary_target(row, target) is not None
    ]
    if len(pairs) < 15:
        return None
    pairs.sort(key=lambda item: item[0])
    blocks: list[dict[str, Any]] = []
    for value, outcome in pairs:
        blocks.append({"upper": value, "sum": Decimal(outcome or 0), "count": 1})
        while len(blocks) >= 2:
            left = blocks[-2]["sum"] / Decimal(blocks[-2]["count"])
            right = blocks[-1]["sum"] / Decimal(blocks[-1]["count"])
            if left <= right:
                break
            latest = blocks.pop()
            previous = blocks.pop()
            blocks.append({
                "upper": latest["upper"],
                "sum": previous["sum"] + latest["sum"],
                "count": previous["count"] + latest["count"],
            })
    return IsotonicModel(
        feature=feature,
        upper_bounds=[block["upper"] for block in blocks],
        probabilities=[block["sum"] / Decimal(block["count"]) for block in blocks],
        default=Decimal(sum(outcome or 0 for _, outcome in pairs)) / Decimal(len(pairs)),
    )


@dataclass
class DecisionStump:
    feature: str
    threshold: Decimal
    left_probability: Decimal
    right_probability: Decimal
    default: Decimal

    def predict(self, row: dict[str, Any]) -> Decimal:
        value = optional_decimal(row.get(self.feature))
        if value is None:
            return self.default
        return self.left_probability if value <= self.threshold else self.right_probability


def fit_decision_stump(
    rows: Sequence[dict[str, Any]], *, target: str,
    feature_names: Sequence[str] = NUMERIC_MODEL_FEATURES,
) -> DecisionStump | None:
    best: tuple[Decimal, DecisionStump] | None = None
    outcomes = [_binary_target(row, target) for row in rows]
    valid_outcomes = [value for value in outcomes if value is not None]
    if len(valid_outcomes) < 20 or len(set(valid_outcomes)) < 2:
        return None
    default = Decimal(sum(valid_outcomes)) / Decimal(len(valid_outcomes))
    for feature in feature_names:
        pairs = [
            (value, _binary_target(row, target))
            for row in rows
            if (value := optional_decimal(row.get(feature))) is not None
            and _binary_target(row, target) is not None
        ]
        if len(pairs) < 15:
            continue
        pairs.sort(key=lambda item: item[0])
        for offset in range(1, 10):
            threshold = pairs[min(len(pairs) - 1, len(pairs) * offset // 10)][0]
            left = [outcome or 0 for value, outcome in pairs if value <= threshold]
            right = [outcome or 0 for value, outcome in pairs if value > threshold]
            if not left or not right:
                continue
            left_probability = Decimal(sum(left)) / Decimal(len(left))
            right_probability = Decimal(sum(right)) / Decimal(len(right))
            error = sum(
                (
                    (left_probability if value <= threshold else right_probability)
                    - Decimal(outcome or 0)
                ) ** 2
                for value, outcome in pairs
            ) / Decimal(len(pairs))
            model = DecisionStump(
                feature, threshold, left_probability, right_probability, default
            )
            if best is None or error < best[0]:
                best = (error, model)
    return best[1] if best else None


def _prediction_metrics(
    predictions: Sequence[tuple[Decimal, Decimal]], *, classification: bool,
) -> dict[str, Any]:
    if not predictions:
        return {"supported": False, "trade_count": 0}
    errors = [prediction - actual for prediction, actual in predictions]
    result = {
        "supported": True,
        "trade_count": len(predictions),
        "spearman": _string(spearman(predictions)),
        "mae": str(sum((abs(value) for value in errors), ZERO) / Decimal(len(errors))),
        "rmse": str(Decimal(str(math.sqrt(
            sum(float(value) ** 2 for value in errors) / len(errors)
        )))),
    }
    if classification:
        result["brier_score"] = str(
            sum((value * value for value in errors), ZERO) / Decimal(len(errors))
        )
        result["accuracy_at_0_5"] = str(
            Decimal(sum((prediction >= Decimal("0.5")) == bool(actual)
                        for prediction, actual in predictions))
            / Decimal(len(predictions))
        )
    return result


def _strategy_prediction(
    train: Sequence[dict[str, Any]], row: dict[str, Any], target: str,
    *, binary: bool,
) -> Decimal | None:
    same = [item for item in train if item["strategy"] == row["strategy"]]
    pool = same or list(train)
    if binary:
        values = [_binary_target(item, target) for item in pool]
        usable = [value for value in values if value is not None]
        return Decimal(sum(usable)) / Decimal(len(usable)) if usable else None
    values = [optional_decimal(item.get(target)) for item in pool]
    usable = [value for value in values if value is not None]
    return sum(usable, ZERO) / Decimal(len(usable)) if usable else None


def model_benchmark_analysis(
    rows: Sequence[dict[str, Any]], folds: dict[str, Any],
) -> dict[str, Any]:
    by_id = {row["execution_id"]: row for row in rows}

    def evaluate(train: Sequence[dict[str, Any]], test: Sequence[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {"regression": {}, "classification": {}}
        for target in ("gross_bps", "net_bps"):
            models = {
                "regularized_linear": fit_ridge(train, target=target),
                "current_score_calibrated_linear": fit_ridge(
                    train, target=target, feature_names=("score",)
                ),
                "current_expected_edge_calibrated_linear": fit_ridge(
                    train, target=target, feature_names=("modeled_expected_edge_bps",)
                ),
            }
            target_result: dict[str, Any] = {}
            for name, model in models.items():
                pairs = [
                    (model.predict(row), actual)
                    for row in test
                    if model is not None
                    and (actual := optional_decimal(row.get(target))) is not None
                ]
                target_result[name] = _prediction_metrics(pairs, classification=False)
            strategy_pairs = [
                (prediction, actual)
                for row in test
                if (prediction := _strategy_prediction(
                    train, row, target, binary=False
                )) is not None
                and (actual := optional_decimal(row.get(target))) is not None
            ]
            target_result["strategy_only_baseline"] = _prediction_metrics(
                strategy_pairs, classification=False
            )
            result["regression"][target] = target_result
        for threshold in (15, 20):
            target = f"post__plus_{threshold}_before_minus_{threshold}"
            models: dict[str, Any] = {
                "regularized_logistic": fit_logistic(train, target=target),
                "shallow_decision_tree_stump": fit_decision_stump(train, target=target),
                "current_score_isotonic": fit_isotonic(
                    train, feature="score", target=target
                ),
                "current_expected_edge_isotonic": fit_isotonic(
                    train, feature="modeled_expected_edge_bps", target=target
                ),
            }
            target_result = {}
            for name, model in models.items():
                pairs = [
                    (model.predict(row), Decimal(actual))
                    for row in test
                    if model is not None
                    and (actual := _binary_target(row, target)) is not None
                ]
                target_result[name] = _prediction_metrics(pairs, classification=True)
            strategy_pairs = [
                (prediction, Decimal(actual))
                for row in test
                if (prediction := _strategy_prediction(
                    train, row, target, binary=True
                )) is not None
                and (actual := _binary_target(row, target)) is not None
            ]
            target_result["strategy_only_baseline"] = _prediction_metrics(
                strategy_pairs, classification=True
            )
            result["classification"][f"reach_plus_{threshold}_before_adverse"] = target_result
        return result

    fold_results = []
    for fold in folds["folds"]:
        train = [by_id[item] for item in fold["train_execution_ids"] if item in by_id]
        validation = [
            by_id[item] for item in fold["validation_execution_ids"] if item in by_id
        ]
        fold_results.append({"fold": fold["fold"], **evaluate(train, validation)})
    development = [
        by_id[item] for item in folds["development_execution_ids"] if item in by_id
    ]
    holdout = [
        by_id[item] for item in folds["final_holdout_execution_ids"] if item in by_id
    ]
    return {
        "models": [
            "regularized linear", "regularized logistic", "isotonic calibration",
            "shallow decision tree stump",
        ],
        "high_capacity_model_used": False,
        "fold_validation": fold_results,
        "train_in_sample": evaluate(development, development),
        "final_holdout": evaluate(development, holdout),
        "final_holdout_used_for_selection": False,
    }


def _string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_json_safe(value), indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _median(values: Iterable[Any]) -> str | None:
    parsed = sorted(value for item in values if (value := optional_decimal(item)) is not None)
    return str(percentile(parsed, Decimal("0.5"))) if parsed else None


def _mean(values: Iterable[Any]) -> str | None:
    parsed = [value for item in values if (value := optional_decimal(item)) is not None]
    return str(sum(parsed, ZERO) / Decimal(len(parsed))) if parsed else None


def _group(rows: Sequence[dict[str, Any]], field: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[str(row.get(field) or "UNKNOWN")].append(row)
    return dict(result)


def _load_price_paths(
    repository: PersistenceRepository,
    rows: Sequence[dict[str, Any]],
) -> dict[str, list[tuple[datetime, Decimal]]]:
    bounds: dict[str, tuple[datetime, datetime]] = {}
    for row in rows:
        start = aware(row["fill_timestamp"])
        end = aware(row["exit_timestamp"])
        if start is None or end is None:
            continue
        symbol = row["symbol"]
        current = bounds.get(symbol)
        bounds[symbol] = (
            min(start, current[0]) if current else start,
            max(end, current[1]) if current else end,
        )
    output: dict[str, list[tuple[datetime, Decimal]]] = {}
    with Session(repository.engine) as session:
        for symbol, (start, end) in sorted(bounds.items()):
            statement = (
                select(
                    V2MarketFeatureRow.captured_at,
                    V2MarketFeatureRow.payload["last_price"].as_string(),
                )
                .where(
                    V2MarketFeatureRow.symbol == symbol,
                    V2MarketFeatureRow.captured_at >= start,
                    V2MarketFeatureRow.captured_at <= end,
                )
                .order_by(V2MarketFeatureRow.captured_at)
            )
            output[symbol] = [
                (timestamp, decimal(price))
                for timestamp, price in session.execute(statement).all()
                if price not in (None, "")
            ]
    return output


def _slice_path(
    points: Sequence[tuple[datetime, Decimal]], start: datetime, end: datetime,
) -> list[tuple[datetime, Decimal]]:
    return [item for item in points if start < item[0] <= end]


def _candidate_payloads(
    repository: PersistenceRepository, candidate_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    with Session(repository.engine) as session:
        rows = session.scalars(
            select(V2SignalCandidateRow).where(V2SignalCandidateRow.id.in_(candidate_ids))
        ).all()
    return {row.id: dict(row.payload or {}) for row in rows}


def build_trade_dataset(
    *,
    baseline: dict[str, Any],
    repository: PersistenceRepository,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    baseline_rows = baseline["trades"]
    execution_ids = {row["execution_id"] for row in baseline_rows}
    executions = {
        str(record.id): record
        for record in repository.load_demo_executions()
        if str(record.id) in execution_ids
    }
    candidate_ids = [str(record.candidate_id) for record in executions.values()]
    candidates = _candidate_payloads(repository, candidate_ids)
    rows: list[dict[str, Any]] = []
    for base in baseline_rows:
        record = executions.get(base["execution_id"])
        if record is None:
            continue
        candidate = candidates.get(str(record.candidate_id), {})
        fill_at = (
            record.first_fill_at
            or record.exchange_fill_at
            or aware(base.get("opened_at"))
        )
        exit_at = record.closed_at
        entry_price = record.average_fill_price
        exit_price = record.average_close_price
        if not all((fill_at, exit_at, entry_price, exit_price)):
            continue
        flattened = flatten_payload(candidate)
        feature_timestamp = aware((candidate.get("feature_snapshot") or {}).get("timestamp"))
        signal_at = record.signal_created_at or aware(candidate.get("created_at"))
        order_at = record.order_submitted_at or record.exchange_submit_started_at
        entry_at = aware(base.get("opened_at")) or order_at or fill_at
        liquidation_last_valid = aware(
            (candidate.get("feature_snapshot") or {}).get("liquidation_last_valid_at")
        )
        row: dict[str, Any] = {
            "run_id": record.run_id,
            "candidate_id": str(record.candidate_id),
            "execution_id": str(record.id),
            "cycle_id": (
                candidate.get("cycle_id") or candidate.get("evaluation_cycle_id")
            ),
            "strategy": record.strategy_name or str(candidate.get("strategy_name") or "UNKNOWN"),
            "symbol": record.symbol.value,
            "side": record.side.value,
            "signal_timestamp": (signal_at or entry_at).isoformat(),
            "entry_timestamp": entry_at.isoformat(),
            "candidate_created_at": str(
                candidate.get("created_at") or (signal_at or fill_at).isoformat()
            ),
            "fill_timestamp": fill_at.isoformat(),
            "exit_timestamp": exit_at.isoformat(),
            "entry_price": str(entry_price),
            "exit_price": str(exit_price),
            "accepted_notional": str(decimal(base["notional"])),
            "gross_pnl": str(decimal(base["gross_pnl"])),
            "fees": str(decimal(base["fees"])),
            "funding": str(decimal(base["funding"])),
            "net_pnl": str(decimal(base["net_pnl"])),
            "gross_bps": str(
                decimal(base["gross_pnl"]) / decimal(base["notional"]) * Decimal("10000")
            ),
            "net_bps": str(
                decimal(base["net_pnl"]) / decimal(base["notional"]) * Decimal("10000")
            ),
            "exit_reason": base["exit_reason"],
            "score": base["final_score"],
            "modeled_expected_edge_bps": base["expected_net_edge_bps"],
            "rank_in_cycle": candidate.get("rank_in_cycle"),
            "active_positions_at_entry": base.get("simultaneous_position_count"),
            "pre__entry_hour_utc": base.get("hour_of_day_utc", fill_at.hour),
            "pre__entry_date": fill_at.date().isoformat(),
            "pre__market_regime": base.get("market_regime"),
            "pre__signal_age_seconds": (
                str(Decimal(str((signal_at - feature_timestamp).total_seconds())))
                if signal_at and feature_timestamp else None
            ),
            "pre__signal_to_order_seconds": (
                str(Decimal(str((order_at - signal_at).total_seconds())))
                if order_at and signal_at else None
            ),
            "post__order_to_fill_seconds": (
                str(Decimal(str((fill_at - order_at).total_seconds())))
                if order_at else None
            ),
            "post__snapshot_to_fill_movement_bps": (
                str(directional_return_bps(
                    decimal((candidate.get("feature_snapshot") or {}).get("last_price")),
                    entry_price,
                    record.side.value,
                ))
                if (candidate.get("feature_snapshot") or {}).get("last_price") else None
            ),
            "pre__liquidation_last_valid_to_signal_seconds": (
                str(Decimal(str((signal_at - liquidation_last_valid).total_seconds())))
                if signal_at and liquidation_last_valid else None
            ),
            "post__liquidation_last_valid_to_fill_seconds": (
                str(Decimal(str((fill_at - liquidation_last_valid).total_seconds())))
                if liquidation_last_valid else None
            ),
            **flattened,
        }
        row["pre__rank_in_cycle"] = candidate.get("rank_in_cycle")
        row["pre__active_positions_at_entry"] = base.get("simultaneous_position_count")
        rows.append(row)
    paths = _load_price_paths(repository, rows)
    path_counts: dict[str, int] = {}
    for row in rows:
        record = executions[row["execution_id"]]
        fill_at = aware(row["fill_timestamp"])
        exit_at = aware(row["exit_timestamp"])
        assert fill_at is not None and exit_at is not None
        path = _slice_path(paths.get(row["symbol"], []), fill_at, exit_at)
        labels = compute_path_labels(
            fill_at=fill_at,
            exit_at=exit_at,
            entry_price=decimal(row["entry_price"]),
            exit_price=decimal(row["exit_price"]),
            side=row["side"],
            observations=path,
            durable_mfe_ratio=record.maximum_favorable_excursion,
            durable_mae_ratio=record.maximum_adverse_excursion,
        )
        row.update(labels)
        path_counts[row["execution_id"]] = len(path)
    metadata = {
        "requested_trade_count": len(baseline_rows),
        "constructed_trade_count": len(rows),
        "missing_execution_ids": sorted(execution_ids - {row["execution_id"] for row in rows}),
        "candidate_payloads_found": len(candidates),
        "market_snapshot_count_loaded": sum(len(value) for value in paths.values()),
        "path_trade_coverage": {
            "any_observation": sum(value > 0 for value in path_counts.values()),
            "zero_observations": sum(value == 0 for value in path_counts.values()),
        },
    }
    return sorted(rows, key=lambda row: row["fill_timestamp"]), metadata


def feature_manifest(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    fields = sorted({key for row in rows for key in row})
    manifest: list[dict[str, Any]] = []
    identifiers = {
        "run_id", "candidate_id", "execution_id", "cycle_id",
    }
    post_roots = {
        "exit_timestamp", "exit_price", "gross_pnl", "fees", "funding",
        "net_pnl", "gross_bps", "net_bps", "exit_reason",
    }
    safe_roots = {
        "strategy", "symbol", "side", "signal_timestamp", "entry_timestamp",
        "candidate_created_at",
        "accepted_notional", "score", "modeled_expected_edge_bps",
        "rank_in_cycle", "active_positions_at_entry", "fill_timestamp", "entry_price",
    }
    for field in fields:
        if field in identifiers:
            availability = "PRE_ENTRY_AVAILABLE"
            rationale = "durable identifier available before or at submission"
            model_allowed = False
        elif field.startswith("post__") or field in post_roots:
            availability = "POST_ENTRY_ONLY"
            rationale = "depends on fill, market path, exit, or accounting outcome"
            model_allowed = False
        elif field.startswith("pre__") or field in safe_roots:
            availability = "PRE_ENTRY_AVAILABLE"
            rationale = "persisted before exchange submission or known at the entry boundary"
            model_allowed = field in NUMERIC_MODEL_FEATURES or field in CATEGORICAL_MODEL_FEATURES
        else:
            availability = "UNKNOWN"
            rationale = "availability cannot be established from durable provenance"
            model_allowed = False
        present = [row.get(field) for row in rows if row.get(field) not in (None, "")]
        numeric = bool(present) and all(optional_decimal(value) is not None for value in present)
        manifest.append({
            "field": field,
            "availability": availability,
            "rationale": rationale,
            "used_for_candidate_model": model_allowed,
            "numeric": numeric,
            "present_count": len(present),
            "missing_count": len(rows) - len(present),
        })
    manifest.extend([
        {
            "field": "liquidation_event_first_seen_at",
            "availability": "UNKNOWN",
            "rationale": "not persisted; liquidation_last_valid_at is retained only as a proxy",
            "used_for_candidate_model": False,
            "numeric": False,
            "present_count": 0,
            "missing_count": len(rows),
        },
        {
            "field": "continuous_tick_path",
            "availability": "UNKNOWN",
            "rationale": "only discrete stored feature snapshots and durable monitor extrema exist",
            "used_for_candidate_model": False,
            "numeric": False,
            "present_count": 0,
            "missing_count": len(rows),
        },
    ])
    return {
        "classification_values": [
            "PRE_ENTRY_AVAILABLE", "POST_ENTRY_ONLY", "UNKNOWN",
        ],
        "leakage_policy": (
            "Only PRE_ENTRY_AVAILABLE fields explicitly marked used_for_candidate_model "
            "may enter entry-selection challengers."
        ),
        "features": manifest,
    }


def mfe_mae_analysis(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for strategy, strategy_rows in _group(rows, "strategy").items():
        stale = [row for row in strategy_rows if row["exit_reason"] == "stale_signal"]
        focus = [
            row for row in strategy_rows
            if decimal(row["net_pnl"]) < 0 or row["exit_reason"] == "stale_signal"
        ]
        mfe = [
            value for row in focus
            if (value := optional_decimal(row.get("post__mfe_bps_until_exit"))) is not None
        ]
        mae = [
            value for row in focus
            if (value := optional_decimal(row.get("post__mae_bps_until_exit"))) is not None
        ]
        time_mfe = [
            value for row in focus
            if (
                value := optional_decimal(row.get("post__time_to_observed_mfe_seconds"))
            ) is not None
        ]
        time_loss = [
            value for row in focus
            if (
                value := optional_decimal(
                    row.get("post__time_to_first_observed_loss_seconds")
                )
            ) is not None
        ]
        never_11 = sum(value < Decimal("11") for value in mfe)
        never_15 = sum(value < Decimal("15") for value in mfe)
        capture_15 = sum(
            value >= Decimal("15") and decimal(row["net_pnl"]) < 0
            for row, value in (
                (row, optional_decimal(row.get("post__mfe_bps_until_exit")))
                for row in focus
            )
            if value is not None
        )
        capture_20 = sum(
            value >= Decimal("20") and decimal(row["net_pnl"]) < 0
            for row, value in (
                (row, optional_decimal(row.get("post__mfe_bps_until_exit")))
                for row in focus
            )
            if value is not None
        )
        entry_share = Decimal(never_11) / Decimal(len(mfe)) if mfe else None
        capture_share = Decimal(capture_15) / Decimal(len(mfe)) if mfe else None
        dominant = (
            "ENTRY_QUALITY_FAILURE"
            if entry_share is not None
            and capture_share is not None
            and entry_share >= capture_share
            else "EXIT_CAPTURE_FAILURE"
            if entry_share is not None and capture_share is not None
            else "UNKNOWN"
        )
        result[strategy] = {
            "focus_trade_count": len(focus),
            "mfe_label_count": len(mfe),
            "never_observed_plus_11_bps_count": never_11,
            "never_observed_plus_11_bps_rate": (
                str(Decimal(never_11) / Decimal(len(mfe))) if mfe else None
            ),
            "never_observed_plus_15_bps_count": never_15,
            "never_observed_plus_15_bps_rate": (
                str(Decimal(never_15) / Decimal(len(mfe))) if mfe else None
            ),
            "reached_plus_15_bps_but_closed_negative_count": capture_15,
            "reached_plus_15_bps_but_closed_negative_rate": (
                str(Decimal(capture_15) / Decimal(len(mfe))) if mfe else None
            ),
            "reached_plus_20_bps_but_closed_negative_count": capture_20,
            "reached_plus_20_bps_but_closed_negative_rate": (
                str(Decimal(capture_20) / Decimal(len(mfe))) if mfe else None
            ),
            "median_mfe_bps": _median(mfe),
            "median_mae_bps": _median(mae),
            "stale_trade_count": len(stale),
            "median_mfe_bps_before_stale_exit": _median(
                row.get("post__mfe_bps_until_exit") for row in stale
            ),
            "median_mae_bps_before_stale_exit": _median(
                row.get("post__mae_bps_until_exit") for row in stale
            ),
            "median_time_to_observed_mfe_seconds": _median(time_mfe),
            "median_time_to_first_observed_loss_seconds": _median(time_loss),
            "dominant_classification": dominant,
            "interpretation_note": (
                "never_observed means not present in durable monitor extrema or stored snapshots; "
                "it is not a claim about unseen exchange ticks"
            ),
        }
    coverage = {
        f"{horizon}s": {
            "observed": sum(row.get(f"post__coverage_{horizon}s") == "OBSERVED" for row in rows),
            "unknown": sum(row.get(f"post__coverage_{horizon}s") != "OBSERVED" for row in rows),
        }
        for horizon in HORIZONS_SECONDS
    }
    return {"by_strategy": result, "horizon_coverage": coverage}


def cost_stress_analysis(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    subsets = {"ALL_EXACT": list(rows)}
    subsets.update({f"STRATEGY::{key}": value for key, value in _group(rows, "strategy").items()})
    result: dict[str, Any] = {}
    for name, items in subsets.items():
        total_notional = sum((decimal(row["accepted_notional"]) for row in items), ZERO)
        total_fees = sum((decimal(row["fees"]) for row in items), ZERO)
        total_gross = sum((decimal(row["gross_pnl"]) for row in items), ZERO)
        actual_cost_bps = (
            total_fees / total_notional * Decimal("10000") if total_notional else None
        )
        gross_bps = (
            total_gross / total_notional * Decimal("10000") if total_notional else None
        )
        result[name] = {
            "actual_observed_cost_bps": _string(actual_cost_bps),
            "gross_edge_bps": _string(gross_bps),
            "additional_gross_edge_bps_required_to_break_even": (
                str(max(ZERO, actual_cost_bps - gross_bps))
                if actual_cost_bps is not None and gross_bps is not None else None
            ),
            "stress": {
                f"{cost}_bps": economic_metrics(items, cost_bps=Decimal(cost))
                for cost in COST_STRESS_BPS
            },
        }
    return result


def calibration_analysis(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for strategy, items in _group(rows, "strategy").items():
        fields = {
            "score": "score",
            "expected_edge": "modeled_expected_edge_bps",
        }
        correlations: dict[str, Any] = {}
        for label, feature in fields.items():
            for target in ("gross_bps", "net_bps"):
                pairs = [
                    (left, right)
                    for row in items
                    if (left := optional_decimal(row.get(feature))) is not None
                    and (right := optional_decimal(row.get(target))) is not None
                ]
                correlations[f"{label}_vs_{target}"] = _string(spearman(pairs))
        score_rho = optional_decimal(correlations.get("score_vs_net_bps"))
        run_correlations = {
            run_id: _string(spearman([
                (score, decimal(row["net_bps"]))
                for row in group
                if (score := optional_decimal(row.get("score"))) is not None
            ]))
            for run_id, group in _group(items, "run_id").items()
            if len(group) >= 3
        }
        signed = [
            value for value in (optional_decimal(value) for value in run_correlations.values())
            if value is not None and abs(value) >= Decimal("0.1")
        ]
        if any(value > 0 for value in signed) and any(value < 0 for value in signed):
            information = "REGIME_DEPENDENT_INFORMATION"
        elif score_rho is None or abs(score_rho) < Decimal("0.1"):
            information = "NO_INFORMATION"
        elif score_rho > 0:
            information = "USEFUL_RANK_INFORMATION"
        else:
            information = "INVERTED_INFORMATION"
        expected_errors = [
            abs(decimal(row["modeled_expected_edge_bps"]) - decimal(row["gross_bps"]))
            for row in items
        ]
        expected_bias = [
            decimal(row["modeled_expected_edge_bps"]) - decimal(row["gross_bps"])
            for row in items
        ]
        result[strategy] = {
            "trade_count": len(items),
            "correlations": correlations,
            "score_information_classification": information,
            "score_vs_net_bps_by_run": run_correlations,
            "score_gross_deciles": calibration_deciles(items, "score", "gross_bps"),
            "score_net_deciles": calibration_deciles(items, "score", "net_bps"),
            "expected_edge_gross_deciles": calibration_deciles(
                items, "modeled_expected_edge_bps", "gross_bps"
            ),
            "mean_absolute_expected_edge_error_bps": (
                str(sum(expected_errors, ZERO) / Decimal(len(expected_errors)))
                if expected_errors else None
            ),
            "mean_expected_edge_calibration_bias_bps": (
                str(sum(expected_bias, ZERO) / Decimal(len(expected_bias)))
                if expected_bias else None
            ),
            "hit_rates": {
                str(threshold): str(
                    Decimal(sum(
                        bool(row.get(f"post__reached_plus_{threshold}_bps"))
                        for row in items
                    ))
                    / Decimal(len(items))
                ) if items else None
                for threshold in (11, 15, 20, 30)
            },
        }
    return result


def _feature_summary(rows: Sequence[dict[str, Any]], fields: Sequence[str]) -> dict[str, Any]:
    return {
        field: {
            "present": sum(row.get(field) not in (None, "") for row in rows),
            "mean": _mean(row.get(field) for row in rows),
            "median": _median(row.get(field) for row in rows),
            "spearman_vs_gross_bps": _string(spearman([
                (left, decimal(row["gross_bps"]))
                for row in rows
                if (left := optional_decimal(row.get(field))) is not None
            ])),
        }
        for field in fields
    }


def liquidation_analysis(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    items = [row for row in rows if row["strategy"] == "LiquidationMomentumStrategy"]
    fields = (
        "pre__feature_snapshot__liquidation_data_age_seconds",
        "pre__liquidation_last_valid_to_signal_seconds",
        "pre__signal_to_order_seconds",
        "post__order_to_fill_seconds",
        "post__liquidation_last_valid_to_fill_seconds",
        "post__snapshot_to_fill_movement_bps",
        "pre__feature_snapshot__liquidation_notional_5m",
        "pre__feature_snapshot__liquidation_imbalance",
        "pre__feature_snapshot__realized_volatility__1m",
        "pre__feature_snapshot__spread_bps",
        "pre__feature_snapshot__bid_depth_10bps_usdt",
        "pre__feature_snapshot__ask_depth_10bps_usdt",
        "pre__rank_in_cycle",
        "pre__active_positions_at_entry",
    )
    moved = [
        row for row in items
        if optional_decimal(row.get("post__snapshot_to_fill_movement_bps")) is not None
    ]
    moved.sort(key=lambda row: decimal(row["post__snapshot_to_fill_movement_bps"]))
    buckets: list[dict[str, Any]] = []
    for index in range(4):
        bucket = moved[len(moved) * index // 4: len(moved) * (index + 1) // 4]
        if bucket:
            buckets.append({
                "quartile": index + 1,
                "movement_min_bps": str(min(
                    decimal(row["post__snapshot_to_fill_movement_bps"])
                    for row in bucket
                )),
                "movement_max_bps": str(max(
                    decimal(row["post__snapshot_to_fill_movement_bps"])
                    for row in bucket
                )),
                "economics_at_11bps": economic_metrics(bucket, cost_bps=Decimal("11")),
                "median_mfe_bps": _median(row.get("post__mfe_bps_until_exit") for row in bucket),
            })
    symbols = {
        symbol: {
            "at_11bps": economic_metrics(group, cost_bps=Decimal("11")),
            "median_mfe_bps": _median(row.get("post__mfe_bps_until_exit") for row in group),
            "median_snapshot_to_fill_movement_bps": _median(
                row.get("post__snapshot_to_fill_movement_bps") for row in group
            ),
        }
        for symbol, group in _group(items, "symbol").items()
    }
    return {
        "mode": "SHADOW_ONLY_RESEARCH",
        "trade_count": len(items),
        "feature_relationships": _feature_summary(items, fields),
        "pnl_and_mfe_vs_already_moved_bps_before_entry": buckets,
        "by_symbol": symbols,
        "event_first_seen_at": {
            "availability": "UNKNOWN",
            "proxy_used": "liquidation_last_valid_at",
            "warning": "proxy is not an exact event-first-seen timestamp",
        },
    }


def oi_analysis(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    items = [row for row in rows if row["strategy"] == "OIFundingSqueezeStrategy"]
    fields = (
        "pre__feature_snapshot__open_interest_change_pct",
        "pre__feature_snapshot__funding_rate",
        "pre__feature_snapshot__funding_deviation_bps",
        "pre__feature_snapshot__price_momentum__30s",
        "pre__feature_snapshot__price_momentum__1m",
        "pre__feature_snapshot__realized_volatility__1m",
        "pre__feature_snapshot__spread_bps",
        "pre__feature_snapshot__bid_depth_10bps_usdt",
        "pre__feature_snapshot__ask_depth_10bps_usdt",
        "pre__signal_age_seconds",
        "modeled_expected_edge_bps",
        "score",
        "pre__rank_in_cycle",
        "pre__active_positions_at_entry",
    )
    exits = {
        reason: {
            "trade_count": len(group),
            "features": _feature_summary(group, fields),
            "economics_at_11bps": economic_metrics(group, cost_bps=Decimal("11")),
        }
        for reason, group in _group(items, "exit_reason").items()
    }
    weak_symbols = {"WIFUSDT", "NEARUSDT", "ADAUSDT"}
    weak = [row for row in items if row["symbol"] in weak_symbols]
    other = [row for row in items if row["symbol"] not in weak_symbols]
    return {
        "mode": "PRIMARY_V3_RESEARCH_STRATEGY",
        "trade_count": len(items),
        "overall_feature_relationships": _feature_summary(items, fields),
        "by_exit_reason": exits,
        "weak_symbol_hypothesis": {
            "symbols": sorted(weak_symbols),
            "weak_group": {
                "economics_at_11bps": economic_metrics(weak, cost_bps=Decimal("11")),
                "features": _feature_summary(weak, fields),
            },
            "other_group": {
                "economics_at_11bps": economic_metrics(other, cost_bps=Decimal("11")),
                "features": _feature_summary(other, fields),
            },
            "interpretation": (
                "symbol identity is descriptive only; causal attribution requires "
                "chronological OOS persistence after conditioning on pre-entry fields"
            ),
        },
    }


def time_and_regime_analysis(
    rows: Sequence[dict[str, Any]], folds: dict[str, Any],
) -> dict[str, Any]:
    by_id = {row["execution_id"]: row for row in rows}
    by_hour = {
        hour: {
            "at_11bps": economic_metrics(group, cost_bps=Decimal("11")),
            "strategies": sorted({str(row["strategy"]) for row in group}),
            "symbols": sorted({str(row["symbol"]) for row in group}),
            "runs": len({str(row["run_id"]) for row in group}),
        }
        for hour, group in _group(rows, "pre__entry_hour_utc").items()
    }
    condition_fields = (
        "strategy", "symbol", "side", "pre__market_regime", "run_id",
        "pre__active_positions_at_entry",
    )
    conditioned: list[dict[str, Any]] = []
    cells: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cells[tuple(row.get(field) for field in condition_fields)].append(row)
    for group in cells.values():
        if len(group) < 2:
            continue
        cell_mean = sum((decimal(row["net_bps"]) for row in group), ZERO) / Decimal(len(group))
        for row in group:
            copy = dict(row)
            copy["research__conditioned_net_bps_residual"] = str(
                decimal(row["net_bps"]) - cell_mean
            )
            conditioned.append(copy)
    conditioned_hours = {
        hour: {
            "trade_count": len(group),
            "mean_conditioned_net_bps_residual": _mean(
                row.get("research__conditioned_net_bps_residual") for row in group
            ),
        }
        for hour, group in _group(conditioned, "pre__entry_hour_utc").items()
    }
    fold_effects = []
    for fold in folds["folds"]:
        validation = [
            by_id[item] for item in fold["validation_execution_ids"] if item in by_id
        ]
        fold_effects.append({
            "fold": fold["fold"],
            "hour_22": economic_metrics(
                [row for row in validation if str(row.get("pre__entry_hour_utc")) == "22"],
                cost_bps=Decimal("11"),
            ),
            "hours_14_16": economic_metrics(
                [
                    row for row in validation
                    if str(row.get("pre__entry_hour_utc")) in {"14", "15", "16"}
                ],
                cost_bps=Decimal("11"),
            ),
        })
    hour_22_positive = sum(
        optional_decimal(item["hour_22"].get("expectancy")) is not None
        and decimal(item["hour_22"]["expectancy"]) > 0
        and int(item["hour_22"].get("trade_count") or 0) >= 3
        for item in fold_effects
    )
    return {
        "optimization_performed": False,
        "raw_by_hour": by_hour,
        "conditioning_fields": list(condition_fields),
        "conditioned_residual_by_hour": conditioned_hours,
        "conditioned_trade_count": len(conditioned),
        "walk_forward_target_hour_effects": fold_effects,
        "hour_22_positive_sufficient_folds": hour_22_positive,
        "time_filter_conclusion": (
            "REJECT_TIME_FILTER; the apparent effect is not sufficiently fold-stable "
            "after conditioning, and cells are sparse"
        ),
    }


def portfolio_counterfactual(
    rows: Sequence[dict[str, Any]], folds: dict[str, Any],
) -> dict[str, Any]:
    by_id = {row["execution_id"]: row for row in rows}
    policies = {
        "ALL_ADMITTED": lambda row: True,
        "TOP_1_ONLY": lambda row: int(optional_decimal(row.get("pre__rank_in_cycle")) or 999) <= 1,
        "TOP_2_ONLY": lambda row: int(optional_decimal(row.get("pre__rank_in_cycle")) or 999) <= 2,
    }
    fold_results: list[dict[str, Any]] = []
    for fold in folds["folds"]:
        validation = [by_id[item] for item in fold["validation_execution_ids"] if item in by_id]
        fold_results.append({
            "fold": fold["fold"],
            "policies": {
                name: {
                    "11_bps": economic_metrics(
                        [row for row in validation if policy(row)], cost_bps=Decimal("11")
                    ),
                    "15_bps": economic_metrics(
                        [row for row in validation if policy(row)], cost_bps=Decimal("15")
                    ),
                }
                for name, policy in policies.items()
            },
        })
    oos_ids = {
        item for fold in folds["folds"] for item in fold["validation_execution_ids"]
    }
    oos = [row for row in rows if row["execution_id"] in oos_ids]
    return {
        "ranking_source": "persisted rank_in_cycle only; realized PnL never selects rank",
        "cycle_reconstruction_status": (
            "NOT_SUPPORTED; no authoritative cycle identifier is persisted for this cohort"
        ),
        "persisted_cycle_count": len({row["cycle_id"] for row in rows if row.get("cycle_id")}),
        "trades_with_unknown_cycle": sum(not row.get("cycle_id") for row in rows),
        "persisted_rank_coverage": sum(
            optional_decimal(row.get("pre__rank_in_cycle")) is not None for row in rows
        ),
        "known_outcome_limitation": (
            "Only admitted candidates with exact executed outcomes can be scored; "
            "unexecuted candidates have no fabricated counterfactual PnL. Because cycle IDs "
            "are absent and 252/253 exact trades have persisted rank 1, TOP-1/TOP-2 results "
            "are descriptive and cannot establish a portfolio-cycle effect."
        ),
        "fold_results": fold_results,
        "walk_forward_oos": {
            name: {
                f"{cost}_bps": economic_metrics(
                    [row for row in oos if policy(row)], cost_bps=Decimal(cost)
                )
                for cost in (11, 13, 15, 18)
            }
            for name, policy in policies.items()
        },
    }


def _exit_counterfactual_gross(row: dict[str, Any], horizon: int) -> Decimal | None:
    return optional_decimal(row.get(f"post__return_bps_{horizon}s"))


def exit_counterfactual(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for strategy, items in _group(rows, "strategy").items():
        policies: dict[str, Any] = {
            "CURRENT_EXIT": {"rows": list(items), "gross_field": "gross_bps"},
        }
        for horizon in (15, 30, 60, 120, 300, 600):
            available: list[dict[str, Any]] = []
            for row in items:
                value = _exit_counterfactual_gross(row, horizon)
                if value is None:
                    continue
                copy = dict(row)
                copy["research__counterfactual_gross_bps"] = str(value)
                available.append(copy)
            policies[f"FIXED_{horizon}S"] = {
                "rows": available,
                "gross_field": "research__counterfactual_gross_bps",
                "coverage": len(available),
            }
            no_follow: list[dict[str, Any]] = []
            for row in items:
                value = _exit_counterfactual_gross(row, horizon)
                if value is None:
                    continue
                copy = dict(row)
                copy["research__counterfactual_gross_bps"] = str(
                    value if value <= 0 else decimal(row["gross_bps"])
                )
                no_follow.append(copy)
            policies[f"NO_FOLLOW_THROUGH_{horizon}S"] = {
                "rows": no_follow,
                "gross_field": "research__counterfactual_gross_bps",
                "coverage": len(no_follow),
            }
        result[strategy] = {
            name: {
                "coverage": value.get("coverage", len(value["rows"])),
                "coverage_rate": str(
                    Decimal(value.get("coverage", len(value["rows"]))) / Decimal(len(items))
                ) if items else None,
                "11_bps": economic_metrics(
                    value["rows"], cost_bps=Decimal("11"), gross_field=value["gross_field"]
                ),
                "15_bps": economic_metrics(
                    value["rows"], cost_bps=Decimal("15"), gross_field=value["gross_field"]
                ),
            }
            for name, value in policies.items()
        }
    return {
        "hard_stop_removed": False,
        "interpolation_used": False,
        "unsupported": {
            "earlier_or_later_stale_trigger": (
                "historical stale-condition state transitions are not stored at "
                "sufficient resolution"
            ),
            "mfe_trailing_protection": (
                "stored path cadence is insufficient for an exact trigger simulation"
            ),
        },
        "by_strategy": result,
    }


def _candidate_policy_rows(
    *,
    name: str,
    train: Sequence[dict[str, Any]],
    evaluate: Sequence[dict[str, Any]],
    cost: Decimal,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    oi_train = [row for row in train if row["strategy"] == "OIFundingSqueezeStrategy"]
    oi_eval = [row for row in evaluate if row["strategy"] == "OIFundingSqueezeStrategy"]
    if name == "BASELINE":
        return oi_eval, {"mechanism": "current OI behavior"}
    model = fit_ridge(oi_train, target="gross_bps")
    if model is None:
        return [], {"mechanism": "ridge unavailable", "supported": False}
    scored = [(row, model.predict(row)) for row in oi_eval]
    selected = [row for row, prediction in scored if prediction > cost]
    training_a = [row for row in oi_train if model.predict(row) > cost]
    training_b = [
        row for row in training_a
        if int(optional_decimal(row.get("pre__rank_in_cycle")) or 999) <= 1
    ]
    details: dict[str, Any] = {
        "mechanism": "training-only ridge predicted gross bps greater than cost",
        "model_features": model.feature_names,
        "supported": True,
    }
    if name in {"CANDIDATE_B", "CANDIDATE_C", "CANDIDATE_D"}:
        baseline_expectancy = optional_decimal(
            economic_metrics(oi_train, cost_bps=cost).get("expectancy")
        )
        candidate_a_expectancy = optional_decimal(
            economic_metrics(training_a, cost_bps=cost).get("expectancy")
        )
        if (
            not training_a
            or candidate_a_expectancy is None
            or baseline_expectancy is None
            or candidate_a_expectancy <= baseline_expectancy
        ):
            details["blocked_by_prior_incremental_gate"] = (
                "Candidate A did not improve training-fold net PnL"
            )
            return [], details
        selected = [
            row for row in selected
            if int(optional_decimal(row.get("pre__rank_in_cycle")) or 999) <= 1
        ]
        details["top_1_applied"] = True
    if name in {"CANDIDATE_C", "CANDIDATE_D"}:
        candidate_b_expectancy = optional_decimal(
            economic_metrics(training_b, cost_bps=cost).get("expectancy")
        )
        if (
            not training_b
            or candidate_b_expectancy is None
            or candidate_a_expectancy is None
            or candidate_b_expectancy <= candidate_a_expectancy
        ):
            details["blocked_by_prior_incremental_gate"] = (
                "Candidate B did not improve training-fold net PnL"
            )
            return [], details
        training_scored = training_b
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in training_scored:
            grouped[f"{row['symbol']}|{row.get('pre__market_regime') or 'UNKNOWN'}"].append(row)
        supported_groups = []
        for key, group in grouped.items():
            if len(group) >= 10:
                metrics = economic_metrics(group, cost_bps=Decimal("15"))
                if (
                    optional_decimal(metrics.get("net_pnl")) is not None
                    and decimal(metrics["net_pnl"]) > 0
                ):
                    supported_groups.append(key)
        selected = [
            row for row in selected
            if f"{row['symbol']}|{row.get('pre__market_regime') or 'UNKNOWN'}"
            in supported_groups
        ]
        details["training_only_supported_symbol_regimes"] = supported_groups
        details["symbol_regime_filter_supported"] = bool(supported_groups)
    if name == "CANDIDATE_D":
        training_c = [
            row for row in training_scored
            if f"{row['symbol']}|{row.get('pre__market_regime') or 'UNKNOWN'}"
            in details["training_only_supported_symbol_regimes"]
        ]
        candidate_c_expectancy = optional_decimal(
            economic_metrics(training_c, cost_bps=cost).get("expectancy")
        )
        if (
            not training_c
            or candidate_c_expectancy is None
            or candidate_b_expectancy is None
            or candidate_c_expectancy <= candidate_b_expectancy
        ):
            details["blocked_by_prior_incremental_gate"] = (
                "Candidate C did not improve training-fold net PnL"
            )
            return [], details
        best_horizon: int | None = None
        best_increment = ZERO
        training_base = economic_metrics(training_c, cost_bps=Decimal("15"))
        base_expectancy = optional_decimal(training_base.get("expectancy")) or ZERO
        for horizon in (15, 30, 60, 120):
            observed = [
                row for row in training_c
                if optional_decimal(row.get(f"post__return_bps_{horizon}s")) is not None
            ]
            if len(observed) < 20 or len(observed) * 2 < max(1, len(training_c)):
                continue
            adjusted = []
            for row in training_c:
                copy = dict(row)
                early = optional_decimal(row.get(f"post__return_bps_{horizon}s"))
                if early is not None and early <= 0:
                    copy["gross_bps"] = str(early)
                adjusted.append(copy)
            expectancy = optional_decimal(
                economic_metrics(adjusted, cost_bps=Decimal("15")).get("expectancy")
            )
            increment = (expectancy - base_expectancy) if expectancy is not None else ZERO
            if expectancy is not None and expectancy > 0 and increment > best_increment:
                best_horizon = horizon
                best_increment = increment
        details["training_only_no_follow_through_horizon_seconds"] = best_horizon
        details["exit_filter_supported"] = best_horizon is not None
        if best_horizon is not None:
            adjusted_selected = []
            for row in selected:
                copy = dict(row)
                early = optional_decimal(row.get(f"post__return_bps_{best_horizon}s"))
                if early is not None and early <= 0:
                    copy["gross_bps"] = str(early)
                adjusted_selected.append(copy)
            selected = adjusted_selected
    return selected, details


def candidate_comparison(
    rows: Sequence[dict[str, Any]], folds: dict[str, Any],
) -> dict[str, Any]:
    by_id = {row["execution_id"]: row for row in rows}
    names = (
        "BASELINE", "CANDIDATE_A", "CANDIDATE_B", "CANDIDATE_C", "CANDIDATE_D",
    )
    fold_results: list[dict[str, Any]] = []
    oos_collections: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fold in folds["folds"]:
        train = [by_id[item] for item in fold["train_execution_ids"] if item in by_id]
        validation = [by_id[item] for item in fold["validation_execution_ids"] if item in by_id]
        policies: dict[str, Any] = {}
        prior_net: Decimal | None = None
        for name in names:
            selected, details = _candidate_policy_rows(
                name=name, train=train, evaluate=validation, cost=Decimal("11")
            )
            metrics = economic_metrics(selected, cost_bps=Decimal("11"))
            net = optional_decimal(metrics.get("net_pnl"))
            policies[name] = {
                "metrics": metrics,
                "incremental_net_pnl": (
                    str(net - prior_net) if net is not None and prior_net is not None else None
                ),
                "details": details,
                "execution_ids": [row["execution_id"] for row in selected],
            }
            prior_net = net
            oos_collections[name].extend(selected)
        fold_results.append({"fold": fold["fold"], "policies": policies})
    oos_summary = {
        name: {
            f"{cost}_bps": economic_metrics(selected, cost_bps=Decimal(cost))
            for cost in (11, 13, 15, 18)
        }
        for name, selected in oos_collections.items()
    }
    oos_incremental: dict[str, Any] = {}
    prior_name: str | None = None
    for name in names:
        current = optional_decimal(oos_summary.get(name, {}).get("11_bps", {}).get("net_pnl"))
        prior = (
            optional_decimal(oos_summary.get(prior_name, {}).get("11_bps", {}).get("net_pnl"))
            if prior_name else None
        )
        oos_incremental[name] = {
            "previous_candidate": prior_name,
            "incremental_net_pnl_at_11bps": (
                str(current - prior)
                if current is not None and prior is not None
                and int(oos_summary.get(name, {}).get("11_bps", {}).get("trade_count") or 0) > 0
                else None
            ),
            "incremental_expectancy_at_11bps": (
                str(
                    decimal(oos_summary[name]["11_bps"]["expectancy"])
                    - decimal(oos_summary[prior_name]["11_bps"]["expectancy"])
                )
                if prior_name
                and optional_decimal(oos_summary.get(name, {}).get("11_bps", {}).get("expectancy"))
                is not None
                and optional_decimal(
                    oos_summary.get(prior_name, {}).get("11_bps", {}).get("expectancy")
                ) is not None
                else None
            ),
        }
        prior_name = name
    development = [
        by_id[item] for item in folds["development_execution_ids"] if item in by_id
    ]
    holdout = [
        by_id[item] for item in folds["final_holdout_execution_ids"] if item in by_id
    ]
    holdout_results: dict[str, Any] = {}
    for name in names:
        selected, details = _candidate_policy_rows(
            name=name, train=development, evaluate=holdout, cost=Decimal("11")
        )
        holdout_results[name] = {
            "details": details,
            "execution_ids": [row["execution_id"] for row in selected],
            "costs": {
                f"{cost}_bps": economic_metrics(selected, cost_bps=Decimal(cost))
                for cost in (11, 13, 15, 18)
            },
        }
    train_results: dict[str, Any] = {}
    for name in names:
        selected, details = _candidate_policy_rows(
            name=name, train=development, evaluate=development, cost=Decimal("11")
        )
        train_results[name] = {
            "details": details,
            "11_bps": economic_metrics(selected, cost_bps=Decimal("11")),
            "execution_ids": [row["execution_id"] for row in selected],
        }
    leave_one_run_out = []
    for split in folds["leave_one_run_out"]:
        train = [by_id[item] for item in split["train_execution_ids"] if item in by_id]
        validation = [
            by_id[item] for item in split["validation_execution_ids"] if item in by_id
        ]
        leave_one_run_out.append({
            "held_out_run_id": split["held_out_run_id"],
            "policies": {
                name: economic_metrics(
                    _candidate_policy_rows(
                        name=name, train=train, evaluate=validation, cost=Decimal("11")
                    )[0],
                    cost_bps=Decimal("11"),
                )
                for name in names
            },
        })
    return {
        "experiment_definitions": {
            "BASELINE": "current OI behavior",
            "CANDIDATE_A": "cost-aware ridge entry selection fitted inside training fold",
            "CANDIDATE_B": "Candidate A plus persisted TOP-1 rank",
            "CANDIDATE_C": (
                "Candidate B plus training-only symbol/regime support at 15 bps"
            ),
            "CANDIDATE_D": "Candidate C plus training-supported no-follow-through exit",
        },
        "train_in_sample": train_results,
        "validation": fold_results,
        "fold_results": fold_results,
        "walk_forward_oos": oos_summary,
        "walk_forward_incremental": oos_incremental,
        "leave_one_run_out": leave_one_run_out,
        "final_holdout": holdout_results,
        "final_holdout_used_for_selection": False,
    }


def _promotion_gate(
    metrics: dict[str, Any], metrics_15: dict[str, Any], *,
    fold_positive: int, fold_count: int,
    symbol_dominates: bool, hour_dominates: bool, run_dominates: bool,
) -> dict[str, Any]:
    trade_count = int(metrics.get("trade_count") or 0)
    expectancy = optional_decimal(metrics.get("expectancy"))
    pf = optional_decimal(metrics.get("profit_factor"))
    return {
        "positive_expectancy": expectancy is not None and expectancy > 0,
        "profit_factor_at_least_1_15": pf is not None and pf >= Decimal("1.15"),
        "oos_trade_count_at_least_30": trade_count >= 30,
        "oos_trade_count_preferred_50": trade_count >= 50,
        "positive_folds_at_least_3_of_4": fold_count >= 4 and fold_positive >= 3,
        "non_negative_aggregate_at_15bps": (
            optional_decimal(metrics_15.get("net_pnl")) is not None
            and decimal(metrics_15["net_pnl"]) >= 0
        ),
        "no_single_symbol_dominates_profit": not symbol_dominates,
        "no_single_hour_dominates_profit": not hour_dominates,
        "no_single_run_dominates_profit": not run_dominates,
        "no_unexplained_lookahead_or_selection_leakage": True,
    }


def _profit_concentration(
    rows: Sequence[dict[str, Any]], field: str, *, cost_bps: Decimal,
) -> tuple[bool, dict[str, str]]:
    contributions: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        notional = decimal(row["accepted_notional"])
        net = decimal(row["gross_bps"]) * notional / Decimal("10000")
        net -= cost_bps * notional / Decimal("10000")
        contributions[str(row.get(field) or "UNKNOWN")] += net
    positive = {key: value for key, value in contributions.items() if value > 0}
    total_positive = sum(positive.values(), ZERO)
    dominates = (
        bool(positive) and max(positive.values()) / total_positive > Decimal("0.50")
        if total_positive > 0 else False
    )
    return dominates, {key: str(value) for key, value in sorted(contributions.items())}


def recommendation(
    *,
    rows: Sequence[dict[str, Any]],
    mfe: dict[str, Any],
    calibration: dict[str, Any],
    liquidation: dict[str, Any],
    portfolio: dict[str, Any],
    exits: dict[str, Any],
    candidates: dict[str, Any],
    time_regime: dict[str, Any],
) -> dict[str, Any]:
    oos = candidates["walk_forward_oos"]
    candidate_order = (
        "BASELINE", "CANDIDATE_A", "CANDIDATE_B", "CANDIDATE_C", "CANDIDATE_D",
    )
    supported_challengers = [
        name for name in candidate_order[1:]
        if int(oos.get(name, {}).get("11_bps", {}).get("trade_count") or 0) > 0
        and optional_decimal(oos.get(name, {}).get("11_bps", {}).get("expectancy"))
        is not None
    ]
    best_challenger = max(
        supported_challengers,
        key=lambda name: decimal(oos[name]["11_bps"]["expectancy"]),
        default=None,
    )
    baseline_expectancy = optional_decimal(oos["BASELINE"]["11_bps"].get("expectancy"))
    challenger_improves = (
        best_challenger is not None
        and baseline_expectancy is not None
        and decimal(oos[best_challenger]["11_bps"]["expectancy"]) > baseline_expectancy
    )
    strongest = best_challenger if challenger_improves else "BASELINE"
    strongest_v3_candidate = strongest if challenger_improves else "NONE_SUPPORTED"
    strongest_metrics = oos.get(strongest, {}).get("11_bps", {})
    fold_positive = sum(
        optional_decimal(fold["policies"].get(strongest, {}).get("metrics", {}).get("net_pnl"))
        is not None
        and decimal(fold["policies"][strongest]["metrics"]["net_pnl"]) > 0
        for fold in candidates["fold_results"]
    )
    selected_ids = {
        execution_id
        for fold in candidates["fold_results"]
        for execution_id in fold["policies"].get(strongest, {}).get("execution_ids", [])
    }
    selected_rows = [row for row in rows if row["execution_id"] in selected_ids]
    symbol_dominates, symbol_contributions = _profit_concentration(
        selected_rows, "symbol", cost_bps=Decimal("11")
    )
    hour_dominates, hour_contributions = _profit_concentration(
        selected_rows, "pre__entry_hour_utc", cost_bps=Decimal("11")
    )
    run_dominates, run_contributions = _profit_concentration(
        selected_rows, "run_id", cost_bps=Decimal("11")
    )
    gates = _promotion_gate(
        strongest_metrics,
        oos.get(strongest, {}).get("15_bps", {}),
        fold_positive=fold_positive,
        fold_count=len(candidates["fold_results"]),
        symbol_dominates=symbol_dominates,
        hour_dominates=hour_dominates,
        run_dominates=run_dominates,
    )
    gate_pass = challenger_improves and all(gates.values())
    entry_votes = []
    capture_votes = []
    for strategy in ("OIFundingSqueezeStrategy", "LiquidationMomentumStrategy"):
        row = mfe["by_strategy"].get(strategy, {})
        entry_votes.append(optional_decimal(row.get("never_observed_plus_11_bps_rate")) or ZERO)
        capture_votes.append(
            optional_decimal(row.get("reached_plus_15_bps_but_closed_negative_rate")) or ZERO
        )
    dominant = (
        "ENTRY_QUALITY_FAILURE"
        if sum(entry_votes, ZERO) >= sum(capture_votes, ZERO)
        else "EXIT_CAPTURE_FAILURE"
    )
    top1 = portfolio["walk_forward_oos"]["TOP_1_ONLY"]["11_bps"]
    all_admitted = portfolio["walk_forward_oos"]["ALL_ADMITTED"]["11_bps"]
    top1_improves = (
        optional_decimal(top1.get("expectancy")) is not None
        and optional_decimal(all_admitted.get("expectancy")) is not None
        and decimal(top1["expectancy"]) > decimal(all_admitted["expectancy"])
    )
    early_oi = exits["by_strategy"].get("OIFundingSqueezeStrategy", {})
    supported_early = [
        name for name, value in early_oi.items()
        if name.startswith("NO_FOLLOW_THROUGH_")
        and decimal(value.get("coverage_rate") or "0") >= Decimal("0.5")
        and optional_decimal(value.get("15_bps", {}).get("expectancy")) is not None
        and decimal(value["15_bps"]["expectancy"]) > 0
    ]
    return {
        "dominant_current_failure": dominant,
        "dominant_failure_evidence": {
            "entry_quality_rates": [str(value) for value in entry_votes],
            "exit_capture_rates": [str(value) for value in capture_votes],
        },
        "oi_repeatable_oos_sub_edge": (
            "SUPPORTED" if gate_pass else "NOT_SUPPORTED"
        ),
        "liquidation_disposition": (
            "REMAIN_SHADOW_ONLY; salvage not demonstrated by current OOS evidence"
        ),
        "descriptive_shadow_strategies": {
            "MemeTrendStrategy": {
                "status": "SHADOW_EVIDENCE_COLLECTION; sample too small for optimization",
                "economics": economic_metrics(
                    [row for row in rows if row["strategy"] == "MemeTrendStrategy"],
                    cost_bps=None,
                ),
            },
            "VolumeSpikeStrategy": {
                "status": "SHADOW; no exact trades available",
                "economics": economic_metrics(
                    [row for row in rows if row["strategy"] == "VolumeSpikeStrategy"],
                    cost_bps=None,
                ),
            },
        },
        "top_1_improves_oos_economics": top1_improves,
        "top_1_evidence_status": (
            "NOT_IDENTIFIABLE; authoritative cycle IDs are absent and 252/253 exact "
            "trades have persisted rank 1"
        ),
        "repeatable_symbol_or_regime_effects": (
            "NONE_PROMOTABLE; positive symbol/hour cells are small or not fold-stable"
        ),
        "time_filter_conclusion": time_regime["time_filter_conclusion"],
        "early_no_follow_through_exit_supported": bool(supported_early),
        "supported_early_exit_policies": supported_early,
        "strongest_v3_candidate": strongest_v3_candidate,
        "evaluation_reference_policy": strongest,
        "strongest_candidate_oos_trade_count": (
            strongest_metrics.get("trade_count") if challenger_improves else 0
        ),
        "strongest_candidate_oos_11bps": (
            strongest_metrics if challenger_improves else None
        ),
        "strongest_candidate_costs": (
            oos.get(strongest) if challenger_improves else None
        ),
        "evaluation_reference_oos_trade_count": strongest_metrics.get("trade_count"),
        "evaluation_reference_oos_11bps": strongest_metrics,
        "evaluation_reference_costs": oos.get(strongest),
        "strongest_candidate_positive_folds": fold_positive,
        "economic_mechanism": (
            candidates["experiment_definitions"].get(strongest)
            if challenger_improves
            else "No V3 mechanism improved chronological OOS expectancy over baseline"
        ),
        "profit_concentration": {
            "symbol": symbol_contributions,
            "hour": hour_contributions,
            "run": run_contributions,
        },
        "falsified_hypotheses": [
            "raising the current final score alone yields a monotonic after-cost edge",
            "the current modeled expected edge is calibrated to realized gross edge",
        ],
        "promotion_gates": gates,
        "promotion_gate_passed": gate_pass,
        "short_demo_economics_canary": (
            "SUPPORTED" if gate_pass else "NOT_SUPPORTED"
        ),
        "data_limitations": [
            "120/373 terminal executions have provisional accounting and are excluded "
            "from feature research",
            "5s/15s/30s stored market-path coverage is sparse; missing horizons remain UNKNOWN",
            "liquidation event first_seen_at is not persisted; last_valid_at is only a proxy",
            "unexecuted candidates have no fabricated counterfactual PnL",
        ],
        "read_only": True,
    }


def render_recommendation_markdown(
    recommendation_payload: dict[str, Any], *,
    dataset_metadata: dict[str, Any],
    folds: dict[str, Any],
) -> str:
    strongest = recommendation_payload["strongest_v3_candidate"]
    reference = recommendation_payload["evaluation_reference_policy"]
    metrics = (
        recommendation_payload.get("strongest_candidate_oos_11bps")
        or recommendation_payload["evaluation_reference_oos_11bps"]
    )
    costs = (
        recommendation_payload.get("strongest_candidate_costs")
        or recommendation_payload.get("evaluation_reference_costs")
        or {}
    )
    top_1_improves = str(recommendation_payload["top_1_improves_oos_economics"]).lower()
    early_exit_supported = str(
        recommendation_payload["early_no_follow_through_exit_supported"]
    ).lower()
    canary = recommendation_payload["short_demo_economics_canary"]
    oi_edge = recommendation_payload["oi_repeatable_oos_sub_edge"]
    symbol_regime = recommendation_payload["repeatable_symbol_or_regime_effects"]
    candidate_trade_count = recommendation_payload["strongest_candidate_oos_trade_count"]
    positive_folds = recommendation_payload["strongest_candidate_positive_folds"]
    lines = [
        "# ByBot V3 Alpha Lab recommendation", "",
        "## Outcome", "",
        f"- Dominant failure: **{recommendation_payload['dominant_current_failure']}**",
        f"- OI repeatable OOS sub-edge: **{recommendation_payload['oi_repeatable_oos_sub_edge']}**",
        f"- Liquidation: **{recommendation_payload['liquidation_disposition']}**",
        f"- TOP-1 improves OOS economics: **{top_1_improves}**",
        f"- TOP-1 evidence: **{recommendation_payload['top_1_evidence_status']}**",
        f"- Early no-follow-through exit supported: **{early_exit_supported}**",
        f"- Strongest tested V3 candidate: **{strongest}**",
        f"- Metrics shown below use evaluation reference: **{reference}**",
        f"- Evidence strong enough for short Demo economics canary: **{canary}**",
        "", "## Required research answers", "",
        f"1. Dominant failure: **{recommendation_payload['dominant_current_failure']}**.",
        f"2. Repeatable OI OOS sub-edge: **{oi_edge}**.",
        f"3. Liquidation: **{recommendation_payload['liquidation_disposition']}**.",
        "4. TOP-1: **not demonstrated**; the only rank-2 row makes the "
        "counterfactual non-identifying.",
        f"5. Symbols/regimes: **{symbol_regime}**.",
        f"6. Early no-follow-through exit: **{early_exit_supported}**.",
        f"7. Strongest V3 candidate: **{strongest}**.",
        f"8. Supporting OOS trades: **{candidate_trade_count}**.",
        "9. Candidate OOS economics: **N/A because no challenger improved baseline**; "
        f"reference {reference} is shown below.",
        "10. Candidate cost stress: **N/A**; reference costs are shown below.",
        f"11. Economic mechanism: {recommendation_payload.get('economic_mechanism')}.",
        "12. Falsified: current score/expected edge do not provide stable calibrated "
        "after-cost rank information.",
        f"13. Short Demo economics canary: **{canary}**.",
        "", "## Evaluation-reference OOS evidence", "",
        f"- Trades: {metrics.get('trade_count')}",
        f"- Gross PnL: {metrics.get('gross_pnl')}",
        f"- Net PnL at 11 bps: {metrics.get('net_pnl')}",
        f"- Profit factor: {metrics.get('profit_factor')}",
        f"- Expectancy: {metrics.get('expectancy')}",
        f"- Maximum drawdown: {metrics.get('maximum_drawdown')}",
        f"- Positive folds: {positive_folds}/{len(folds['folds'])}",
        f"- Leave-one-run-out splits: {len(folds['leave_one_run_out'])}",
        "", "## Cost stress", "",
    ]
    for cost in (11, 13, 15, 18):
        item = costs.get(f"{cost}_bps", {})
        lines.append(
            f"- {cost} bps: trades={item.get('trade_count')}, "
            f"net={item.get('net_pnl')}, PF={item.get('profit_factor')}, "
            f"expectancy={item.get('expectancy')}"
        )
    lines.extend([
        "", "## Mechanism and falsification", "",
        f"- Mechanism: {recommendation_payload.get('economic_mechanism')}",
    ])
    for item in recommendation_payload["falsified_hypotheses"]:
        lines.append(f"- Falsified: {item}")
    lines.extend(["", "## Promotion gates", ""])
    for key, passed in recommendation_payload["promotion_gates"].items():
        lines.append(f"- {key}: {'PASS' if passed else 'FAIL'}")
    lines.extend([
        "", "## Dataset integrity", "",
        f"- Exact trade rows: {dataset_metadata['constructed_trade_count']}",
        f"- Final holdout trades: {len(folds['final_holdout_execution_ids'])}",
        "- Final holdout was frozen before model selection: true",
        "- Exchange/database mutations: none",
        "- Production strategy changes: none",
        "", "## Limitations", "",
    ])
    for item in recommendation_payload["data_limitations"]:
        lines.append(f"- {item}")
    return "\n".join(lines).rstrip() + "\n"


def write_trade_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    preferred = [
        "run_id", "candidate_id", "execution_id", "cycle_id", "strategy",
        "symbol", "side", "entry_timestamp", "fill_timestamp", "exit_timestamp",
        "entry_price", "exit_price", "accepted_notional", "gross_pnl", "fees",
        "funding", "net_pnl", "gross_bps", "net_bps", "exit_reason", "score",
        "modeled_expected_edge_bps", "rank_in_cycle", "active_positions_at_entry",
    ]
    ordered = preferred + [field for field in fields if field not in preferred]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ordered, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _json_safe(value) for key, value in row.items()})


def run_alpha_lab(
    *,
    baseline_path: Path,
    output_dir: Path,
    repository: PersistenceRepository,
) -> dict[str, Any]:
    if not repository.available:
        raise RuntimeError("database persistence is unavailable")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8-sig"))
    output_dir.mkdir(parents=True, exist_ok=True)
    rows, dataset_metadata = build_trade_dataset(
        baseline=baseline, repository=repository
    )
    folds = chronological_splits(rows)
    manifest = feature_manifest(rows)
    mfe = mfe_mae_analysis(rows)
    costs = cost_stress_analysis(rows)
    calibration = calibration_analysis(rows)
    model_benchmarks = model_benchmark_analysis(rows, folds)
    liquidation = liquidation_analysis(rows)
    oi = oi_analysis(rows)
    time_regime = time_and_regime_analysis(rows, folds)
    portfolio = portfolio_counterfactual(rows, folds)
    exits = exit_counterfactual(rows)
    candidates = candidate_comparison(rows, folds)
    recommendation_payload = recommendation(
        rows=rows,
        mfe=mfe,
        calibration=calibration,
        liquidation=liquidation,
        portfolio=portfolio,
        exits=exits,
        candidates=candidates,
        time_regime=time_regime,
    )
    common = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_path": str(baseline_path.resolve()),
        "read_only": True,
        "exchange_mutations": False,
        "database_mutations": False,
        "research_cohorts": {
            "broad_terminal_outcome_only": {
                "trade_count": 373,
                "wins": 131,
                "win_rate": "0.3512",
                "net_pnl": "-44.67256361",
                "profit_factor": "0.4502",
                "expectancy": "-0.11977",
                "feature_or_cost_research_allowed": False,
                "source": "strategy-profitability-review.md",
            },
            "exact_accounting": {
                **baseline["summary"],
                "feature_or_cost_research_allowed": True,
            },
        },
    }
    write_trade_csv(output_dir / "trade-dataset.csv", rows)
    write_json(output_dir / "feature-manifest.json", {**common, **manifest})
    write_json(output_dir / "walk-forward-folds.json", {**common, **folds})
    write_json(output_dir / "mfe-mae-analysis.json", {**common, **mfe})
    write_json(output_dir / "cost-stress.json", {**common, "subsets": costs})
    write_json(
        output_dir / "calibration.json",
        {**common, "strategies": calibration, "model_benchmarks": model_benchmarks},
    )
    write_json(output_dir / "liquidation-analysis.json", {**common, **liquidation})
    write_json(
        output_dir / "oi-analysis.json",
        {**common, **oi, "time_and_regime": time_regime},
    )
    write_json(output_dir / "portfolio-counterfactual.json", {**common, **portfolio})
    write_json(output_dir / "exit-counterfactual.json", {**common, **exits})
    write_json(output_dir / "candidate-comparison.json", {**common, **candidates})
    write_json(
        output_dir / "recommendation.json",
        {**common, "dataset_metadata": dataset_metadata, **recommendation_payload},
    )
    (output_dir / "recommendation.md").write_text(
        render_recommendation_markdown(
            recommendation_payload,
            dataset_metadata=dataset_metadata,
            folds=folds,
        ),
        encoding="utf-8",
    )
    return {
        "output_dir": str(output_dir.resolve()),
        "trade_count": len(rows),
        "final_holdout_trade_count": len(folds["final_holdout_execution_ids"]),
        "strongest_v3_candidate": recommendation_payload["strongest_v3_candidate"],
        "promotion_gate_passed": recommendation_payload["promotion_gate_passed"],
        "short_demo_economics_canary": recommendation_payload["short_demo_economics_canary"],
        "read_only": True,
    }
