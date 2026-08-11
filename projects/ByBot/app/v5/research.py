from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import math
from statistics import mean
from typing import Any, Iterable, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from app.v5.models import (
    CarryLabel,
    CarryOpportunity,
    DataAvailability,
    ExecutionScenario,
    FundingPayment,
    MarketLegSnapshot,
)


ZERO = Decimal("0")
ONE = Decimal("1")
TEN_THOUSAND = Decimal("10000")
LONG_HORIZONS_SECONDS = (1800, 3600, 7200, 14400, 28800, 43200, 86400)
COST_STRESS_MULTIPLIERS = (
    Decimal("1"), Decimal("1.25"), Decimal("1.50"), Decimal("2"),
)


def decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ArithmeticError, ValueError):
        return None


def midpoint(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
        return None
    return (bid + ask) / Decimal("2")


def spread_bps(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    mid = midpoint(bid, ask)
    if mid is None:
        return None
    return (ask - bid) / mid * TEN_THOUSAND


def calculate_basis_bps(spot_mid: Decimal, perp_mid: Decimal) -> Decimal:
    if spot_mid <= 0 or perp_mid <= 0:
        raise ValueError("basis prices must be positive")
    return (perp_mid / spot_mid - ONE) * TEN_THOUSAND


def deterministic_carry_id(
    *, symbol: str, spot_symbol: str, perp_symbol: str, timestamp: datetime,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "|".join((
            "bybot-v5-carry", symbol, spot_symbol, perp_symbol,
            timestamp.astimezone(timezone.utc).isoformat(),
        )),
    )


def funding_cashflow(
    *, notional: Decimal, rate: Decimal, perp_side: str = "SHORT",
) -> Decimal:
    """Positive rate pays a short perpetual and charges a long perpetual."""
    if notional < 0:
        raise ValueError("funding notional cannot be negative")
    side = perp_side.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("perp_side must be LONG or SHORT")
    return notional * rate * (ONE if side == "SHORT" else Decimal("-1"))


def build_carry_opportunity(
    *,
    symbol: str,
    spot: MarketLegSnapshot,
    perp: MarketLegSnapshot,
    current_funding_rate: Decimal | None = None,
    predicted_funding_rate: Decimal | None = None,
    next_funding_time: datetime | None = None,
    funding_interval_hours: Decimal | None = None,
    historical_funding: dict[str, Decimal | None] | None = None,
    funding_persistence: dict[str, Decimal | None] | None = None,
    account_fees_bps: dict[str, Decimal | None] | None = None,
    max_alignment_ms: Decimal = Decimal("2000"),
    source: str = "V5_CARRY_SHADOW",
) -> CarryOpportunity:
    if spot.category != "spot" or perp.category != "linear":
        raise ValueError("carry requires one spot and one linear perpetual snapshot")
    timestamp = max(spot.received_at, perp.received_at)
    alignment = Decimal(str(abs((spot.source_timestamp - perp.source_timestamp).total_seconds()))) * Decimal("1000")
    spot_mid = spot.mid
    perp_mid = perp.mid
    fees = account_fees_bps or {
        "spot_maker": None,
        "spot_taker": None,
        "perp_maker": None,
        "perp_taker": None,
    }
    expected_fee_keys = ("spot_maker", "spot_taker", "perp_maker", "perp_taker")
    fees = {key: decimal_or_none(fees.get(key)) for key in expected_fee_keys}
    blockers: list[str] = []
    if alignment > max_alignment_ms:
        blockers.append("SPOT_PERP_TIMESTAMP_MISALIGNED")
    if spot_mid is None or perp_mid is None:
        blockers.append("SPOT_OR_PERP_BOOK_INCOMPLETE")
    if current_funding_rate is None:
        blockers.append("CURRENT_FUNDING_MISSING")
    if predicted_funding_rate is None:
        blockers.append("PREDICTED_FUNDING_MISSING")
    if next_funding_time is None or funding_interval_hours is None:
        blockers.append("FUNDING_SCHEDULE_MISSING")
    if any(fees[key] is None for key in expected_fee_keys):
        blockers.append("ACCOUNT_FEE_CONFIGURATION_MISSING")
    hist = historical_funding or {}
    if any(hist.get(key) is None for key in ("previous_period", "rolling_24h", "rolling_3d", "rolling_7d")):
        blockers.append("HISTORICAL_FUNDING_INCOMPLETE")

    basis = (
        calculate_basis_bps(spot_mid, perp_mid)
        if spot_mid is not None and perp_mid is not None else None
    )
    availability = {
        "spot_book": DataAvailability.AVAILABLE if spot_mid is not None else DataAvailability.MISSING,
        "perp_book": DataAvailability.AVAILABLE if perp_mid is not None else DataAvailability.MISSING,
        "mark_index": (
            DataAvailability.AVAILABLE
            if perp.mark_price is not None and perp.index_price is not None
            else DataAvailability.PARTIAL
            if perp.mark_price is not None or perp.index_price is not None
            else DataAvailability.MISSING
        ),
        "predicted_funding": (
            DataAvailability.AVAILABLE
            if predicted_funding_rate is not None else DataAvailability.MISSING
        ),
        "funding_history": (
            DataAvailability.AVAILABLE
            if "HISTORICAL_FUNDING_INCOMPLETE" not in blockers else DataAvailability.PARTIAL
            if hist else DataAvailability.MISSING
        ),
        "account_fees": (
            DataAvailability.AVAILABLE
            if "ACCOUNT_FEE_CONFIGURATION_MISSING" not in blockers
            else DataAvailability.MISSING
        ),
        "aligned_prices": (
            DataAvailability.AVAILABLE
            if alignment <= max_alignment_ms and spot_mid is not None and perp_mid is not None
            else DataAvailability.MISSING
        ),
    }
    return CarryOpportunity(
        opportunity_id=deterministic_carry_id(
            symbol=symbol,
            spot_symbol=spot.symbol,
            perp_symbol=perp.symbol,
            timestamp=timestamp,
        ),
        timestamp=timestamp,
        symbol=symbol,
        spot_symbol=spot.symbol,
        perp_symbol=perp.symbol,
        spot_source_timestamp=spot.source_timestamp,
        perp_source_timestamp=perp.source_timestamp,
        alignment_ms=alignment,
        spot_bid=spot.bid,
        spot_ask=spot.ask,
        spot_mid=spot_mid,
        perp_bid=perp.bid,
        perp_ask=perp.ask,
        perp_mid=perp_mid,
        mark_price=perp.mark_price,
        index_price=perp.index_price,
        basis_bps=basis,
        current_funding_rate=current_funding_rate,
        predicted_funding_rate=predicted_funding_rate,
        next_funding_time=next_funding_time,
        funding_interval_hours=funding_interval_hours,
        historical_funding=hist,
        funding_persistence=funding_persistence or {},
        spot_spread_bps=spread_bps(spot.bid, spot.ask),
        perp_spread_bps=spread_bps(perp.bid, perp.ask),
        spot_bid_depth_usdt=spot.bid_depth_usdt,
        spot_ask_depth_usdt=spot.ask_depth_usdt,
        perp_bid_depth_usdt=perp.bid_depth_usdt,
        perp_ask_depth_usdt=perp.ask_depth_usdt,
        spot_slippage_bps=spot.slippage_bps_by_notional,
        perp_slippage_bps=perp.slippage_bps_by_notional,
        account_fees_bps=fees,
        expected_costs_bps={},
        availability=availability,
        blockers=list(dict.fromkeys(blockers)),
        source=source,
    )


@dataclass(frozen=True)
class CarryPathPoint:
    timestamp: datetime
    spot_bid: Decimal
    spot_ask: Decimal
    perp_bid: Decimal
    perp_ask: Decimal

    @property
    def spot_mid(self) -> Decimal:
        return (self.spot_bid + self.spot_ask) / Decimal("2")

    @property
    def perp_mid(self) -> Decimal:
        return (self.perp_bid + self.perp_ask) / Decimal("2")


def _slippage_for(
    values: dict[str, Decimal | None], notional: Decimal,
) -> Decimal | None:
    direct = decimal_or_none(values.get(str(notional)))
    if direct is not None:
        return direct
    direct = decimal_or_none(values.get(format(notional, "f")))
    return direct


def execution_scenarios(
    opportunity: CarryOpportunity, *, notional_usdt: Decimal,
) -> dict[str, dict[str, Any]]:
    """Scenario costs. Maker cases stay insufficient without actual fill telemetry."""
    spot_slippage = _slippage_for(opportunity.spot_slippage_bps, notional_usdt)
    perp_slippage = _slippage_for(opportunity.perp_slippage_bps, notional_usdt)
    result: dict[str, dict[str, Any]] = {}
    mapping = {
        ExecutionScenario.TAKER_TAKER: ("spot_taker", "perp_taker", False),
        ExecutionScenario.MAKER_TAKER: ("spot_maker", "perp_taker", True),
        ExecutionScenario.TAKER_MAKER: ("spot_taker", "perp_maker", True),
        ExecutionScenario.MAKER_MAKER: ("spot_maker", "perp_maker", True),
        ExecutionScenario.MAKER_WITH_BOUNDED_TAKER_FALLBACK: (
            "spot_maker", "perp_maker", True,
        ),
    }
    for scenario, (spot_key, perp_key, maker_required) in mapping.items():
        spot_fee = decimal_or_none(opportunity.account_fees_bps.get(spot_key))
        perp_fee = decimal_or_none(opportunity.account_fees_bps.get(perp_key))
        missing = []
        if spot_fee is None or perp_fee is None:
            missing.append("ACCOUNT_FEES")
        if spot_slippage is None or perp_slippage is None:
            missing.append("SLIPPAGE")
        if maker_required:
            missing.extend(("MAKER_FILL_PROBABILITY", "MAKER_ADVERSE_SELECTION"))
        status = "INSUFFICIENT_EXECUTION_DATA" if missing else "MODELED"
        round_trip_bps = None
        if not missing:
            round_trip_bps = (spot_fee + perp_fee + spot_slippage + perp_slippage) * Decimal("2")
        result[scenario.value] = {
            "status": status,
            "round_trip_cost_bps": str(round_trip_bps) if round_trip_bps is not None else None,
            "missing": missing,
            "maker_fill_assumed": False,
            "notional_usdt": str(notional_usdt),
        }
    return result


def build_carry_label(
    opportunity: CarryOpportunity,
    path: Sequence[CarryPathPoint],
    funding: Sequence[FundingPayment],
    *,
    horizon: str,
    horizon_end: datetime,
    notional_usdt: Decimal,
) -> CarryLabel:
    blockers: list[str] = []
    required_prices = (
        opportunity.spot_ask, opportunity.perp_bid,
    )
    if any(value is None for value in required_prices):
        blockers.append("ENTRY_BOOK_INCOMPLETE")
    eligible = sorted(
        (point for point in path if opportunity.timestamp < point.timestamp <= horizon_end),
        key=lambda point: point.timestamp,
    )
    if not eligible:
        blockers.append("EXIT_PATH_MISSING")
    fee_keys = ("spot_taker", "perp_taker")
    fees = [decimal_or_none(opportunity.account_fees_bps.get(key)) for key in fee_keys]
    if any(value is None for value in fees):
        blockers.append("ACCOUNT_TAKER_FEES_MISSING")
    spot_slip = _slippage_for(opportunity.spot_slippage_bps, notional_usdt)
    perp_slip = _slippage_for(opportunity.perp_slippage_bps, notional_usdt)
    if spot_slip is None or perp_slip is None:
        blockers.append("SLIPPAGE_ESTIMATE_MISSING")
    if blockers:
        return CarryLabel(
            opportunity_id=opportunity.opportunity_id,
            symbol=opportunity.symbol,
            horizon=horizon,
            horizon_end=horizon_end,
            coverage=DataAvailability.UNKNOWN,
            blockers=blockers,
        )

    entry_spot = opportunity.spot_ask  # type: ignore[assignment]
    entry_perp = opportunity.perp_bid  # type: ignore[assignment]
    end = eligible[-1]
    spot_quantity = notional_usdt / entry_spot
    perp_quantity = notional_usdt / entry_perp
    spot_pnl = spot_quantity * (end.spot_bid - entry_spot)
    perp_pnl = perp_quantity * (entry_perp - end.perp_ask)
    funding_rows = [
        item for item in funding
        if opportunity.timestamp < item.timestamp <= horizon_end and item.authoritative
    ]
    funding_flows = [
        funding_cashflow(
            notional=perp_quantity * entry_perp,
            rate=item.rate,
            perp_side="SHORT",
        )
        for item in funding_rows
    ]
    funding_income = sum(funding_flows, ZERO)
    entry_fee = (
        notional_usdt * fees[0] / TEN_THOUSAND
        + notional_usdt * fees[1] / TEN_THOUSAND
    )
    exit_spot_notional = spot_quantity * end.spot_bid
    exit_perp_notional = perp_quantity * end.perp_ask
    exit_fee = (
        exit_spot_notional * fees[0] / TEN_THOUSAND
        + exit_perp_notional * fees[1] / TEN_THOUSAND
    )
    slippage = (
        notional_usdt * (spot_slip + perp_slip) / TEN_THOUSAND * Decimal("2")
    )
    hedged_gross = spot_pnl + perp_pnl + funding_income
    net = hedged_gross - entry_fee - exit_fee - slippage
    entry_basis = calculate_basis_bps(opportunity.spot_mid, opportunity.perp_mid)  # type: ignore[arg-type]
    exit_basis = calculate_basis_bps(end.spot_mid, end.perp_mid)
    basis_change_pnl = -(exit_basis - entry_basis) * notional_usdt / TEN_THOUSAND
    basis_path = [calculate_basis_bps(point.spot_mid, point.perp_mid) for point in eligible]
    maximum_basis_adverse = max([ZERO] + [value - entry_basis for value in basis_path])
    hedge_imbalances = [
        abs(
            spot_quantity * point.spot_mid - perp_quantity * point.perp_mid
        ) / notional_usdt * TEN_THOUSAND
        for point in eligible
    ]
    initial_sign = (
        1 if opportunity.current_funding_rate is not None and opportunity.current_funding_rate > 0
        else -1 if opportunity.current_funding_rate is not None and opportunity.current_funding_rate < 0
        else 0
    )
    funding_flip = any(
        (1 if item.rate > 0 else -1 if item.rate < 0 else 0) not in {0, initial_sign}
        for item in funding_rows
    ) if initial_sign else None

    break_even: Decimal | None = None
    for point in eligible:
        interim_spot = spot_quantity * (point.spot_bid - entry_spot)
        interim_perp = perp_quantity * (entry_perp - point.perp_ask)
        interim_funding = sum(
            (
                funding_cashflow(
                    notional=notional_usdt, rate=item.rate, perp_side="SHORT"
                )
                for item in funding_rows if item.timestamp <= point.timestamp
            ),
            ZERO,
        )
        interim_exit_fee = (
            spot_quantity * point.spot_bid * fees[0] / TEN_THOUSAND
            + perp_quantity * point.perp_ask * fees[1] / TEN_THOUSAND
        )
        if interim_spot + interim_perp + interim_funding - entry_fee - interim_exit_fee - slippage >= 0:
            break_even = Decimal(str((point.timestamp - opportunity.timestamp).total_seconds()))
            break

    return CarryLabel(
        opportunity_id=opportunity.opportunity_id,
        symbol=opportunity.symbol,
        horizon=horizon,
        horizon_end=horizon_end,
        coverage=DataAvailability.AVAILABLE,
        funding_income=funding_income,
        basis_change_pnl=basis_change_pnl,
        spot_leg_pnl=spot_pnl,
        perp_leg_pnl=perp_pnl,
        hedged_gross_pnl=hedged_gross,
        entry_cost=entry_fee,
        exit_cost=exit_fee,
        estimated_slippage=slippage,
        funding_received=sum((value for value in funding_flows if value > 0), ZERO),
        funding_paid=abs(sum((value for value in funding_flows if value < 0), ZERO)),
        net_carry_pnl=net,
        net_carry_bps=net / notional_usdt * TEN_THOUSAND,
        max_basis_adverse_excursion_bps=maximum_basis_adverse,
        max_hedge_imbalance_bps=max(hedge_imbalances, default=ZERO),
        funding_sign_flip=funding_flip,
        time_to_break_even_seconds=break_even,
        details={
            "entry_basis_bps": str(entry_basis),
            "exit_basis_bps": str(exit_basis),
            "funding_payment_count": len(funding_rows),
            "spot_quantity": str(spot_quantity),
            "perp_quantity": str(perp_quantity),
        },
    )


@dataclass(frozen=True)
class PricePoint:
    timestamp: datetime
    price: Decimal
    funding_rate: Decimal | None = None
    spread_bps: Decimal | None = None
    depth_usdt: Decimal | None = None


@dataclass(frozen=True)
class MomentumObservation:
    observation_id: str
    symbol: str
    timestamp: datetime
    horizon_seconds: int
    past_return_bps: Decimal
    side: str
    future_return_bps: Decimal
    gross_strategy_bps: Decimal
    reference_cost_bps: Decimal
    net_strategy_bps: Decimal


def _near_before(
    points: Sequence[PricePoint], times: Sequence[datetime], target: datetime,
    tolerance_seconds: int,
) -> PricePoint | None:
    index = bisect_right(times, target) - 1
    if index < 0:
        return None
    point = points[index]
    return point if (target - point.timestamp).total_seconds() <= tolerance_seconds else None


def _near_after(
    points: Sequence[PricePoint], times: Sequence[datetime], target: datetime,
    tolerance_seconds: int,
) -> PricePoint | None:
    index = bisect_left(times, target)
    if index >= len(points):
        return None
    point = points[index]
    return point if (point.timestamp - target).total_seconds() <= tolerance_seconds else None


def build_non_overlapping_momentum(
    symbol: str,
    points: Sequence[PricePoint],
    *,
    horizon_seconds: int,
    reference_cost_bps: Decimal = Decimal("11"),
    endpoint_tolerance_seconds: int = 180,
) -> list[MomentumObservation]:
    ordered = sorted((point for point in points if point.price > 0), key=lambda row: row.timestamp)
    if len(ordered) < 3:
        return []
    times = [point.timestamp for point in ordered]
    start = ordered[0].timestamp + timedelta(seconds=horizon_seconds)
    end = ordered[-1].timestamp - timedelta(seconds=horizon_seconds)
    anchor = start
    output: list[MomentumObservation] = []
    while anchor <= end:
        current = _near_before(ordered, times, anchor, endpoint_tolerance_seconds)
        past = _near_before(
            ordered, times, anchor - timedelta(seconds=horizon_seconds),
            endpoint_tolerance_seconds,
        )
        future = _near_after(
            ordered, times, anchor + timedelta(seconds=horizon_seconds),
            endpoint_tolerance_seconds,
        )
        if current is not None and past is not None and future is not None:
            past_return = (current.price / past.price - ONE) * TEN_THOUSAND
            future_return = (future.price / current.price - ONE) * TEN_THOUSAND
            side = "BUY" if past_return >= 0 else "SELL"
            gross = future_return if side == "BUY" else -future_return
            digest = hashlib.sha256(
                f"{symbol}|{horizon_seconds}|{anchor.isoformat()}".encode("utf-8")
            ).hexdigest()[:24]
            output.append(MomentumObservation(
                observation_id=digest,
                symbol=symbol,
                timestamp=anchor,
                horizon_seconds=horizon_seconds,
                past_return_bps=past_return,
                side=side,
                future_return_bps=future_return,
                gross_strategy_bps=gross,
                reference_cost_bps=reference_cost_bps,
                net_strategy_bps=gross - reference_cost_bps,
            ))
        anchor += timedelta(seconds=horizon_seconds)
    return output


def chronological_folds(
    rows: Sequence[Any], *, timestamp_getter=lambda row: row.timestamp,
    folds: int = 4, holdout_fraction: Decimal = Decimal("0.20"),
    purge_seconds: int = 0,
) -> dict[str, Any]:
    ordered = sorted(rows, key=timestamp_getter)
    if not ordered:
        return {
            "method": "expanding_chronological",
            "holdout_frozen": True,
            "development": [], "holdout": [], "folds": [],
        }
    holdout_count = max(1, int(Decimal(len(ordered)) * holdout_fraction))
    development = ordered[:-holdout_count]
    holdout = ordered[-holdout_count:]
    segment = max(1, len(development) // (folds + 1))
    result_folds = []
    for number in range(1, folds + 1):
        validation_start_index = number * segment
        validation_end_index = (
            len(development) if number == folds
            else min(len(development), (number + 1) * segment)
        )
        validation = development[validation_start_index:validation_end_index]
        if not validation:
            continue
        cutoff = timestamp_getter(validation[0]) - timedelta(seconds=purge_seconds)
        train = [row for row in development[:validation_start_index] if timestamp_getter(row) < cutoff]
        result_folds.append({
            "fold": number,
            "train": train,
            "validation": validation,
            "train_end": timestamp_getter(train[-1]).isoformat() if train else None,
            "validation_start": timestamp_getter(validation[0]).isoformat(),
            "fit_scope": "TRAIN_ONLY",
        })
    return {
        "method": "expanding_chronological_with_label_purge",
        "holdout_frozen": True,
        "holdout_used_for_selection": False,
        "development": development,
        "holdout": holdout,
        "holdout_start": timestamp_getter(holdout[0]).isoformat(),
        "folds": result_folds,
    }


def economic_metrics(
    gross_values: Sequence[Decimal], *, cost_bps: Decimal,
) -> dict[str, Any]:
    gross = list(gross_values)
    net = [value - cost_bps for value in gross]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value < 0]
    equity = ZERO
    peak = ZERO
    max_drawdown = ZERO
    for value in net:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "observation_count": len(net),
        "gross_expectancy_bps": str(sum(gross, ZERO) / Decimal(len(gross))) if gross else None,
        "net_expectancy_bps": str(sum(net, ZERO) / Decimal(len(net))) if net else None,
        "win_rate": str(Decimal(len(wins)) / Decimal(len(net))) if net else None,
        "profit_factor": (
            str(sum(wins, ZERO) / abs(sum(losses, ZERO))) if losses else None
        ),
        "maximum_drawdown_bps": str(max_drawdown),
        "cost_bps": str(cost_bps),
    }


def cost_stress(
    gross_values: Sequence[Decimal], *, base_cost_bps: Decimal,
) -> dict[str, Any]:
    return {
        f"{multiplier}x": economic_metrics(
            gross_values, cost_bps=base_cost_bps * multiplier
        )
        for multiplier in COST_STRESS_MULTIPLIERS
    }


def fit_beta_train_only(
    asset_returns: Sequence[Decimal], market_returns: Sequence[Decimal],
) -> Decimal | None:
    if len(asset_returns) != len(market_returns) or len(asset_returns) < 3:
        return None
    x = [float(value) for value in market_returns]
    y = [float(value) for value in asset_returns]
    x_mean = mean(x)
    y_mean = mean(y)
    variance = sum((value - x_mean) ** 2 for value in x)
    if variance <= 0:
        return None
    covariance = sum((left - x_mean) * (right - y_mean) for left, right in zip(x, y))
    result = Decimal(str(covariance / variance))
    return result if result.is_finite() else None


def beta_hedged_pair_return_bps(
    *, long_return_bps: Decimal, short_return_bps: Decimal,
    long_beta: Decimal, short_beta: Decimal,
) -> tuple[Decimal, Decimal]:
    if abs(short_beta) < Decimal("0.05"):
        raise ValueError("short-leg beta is too close to zero")
    hedge_ratio = long_beta / short_beta
    if hedge_ratio <= 0:
        raise ValueError("positive beta hedge ratio is required")
    return long_return_bps - hedge_ratio * short_return_bps, hedge_ratio


def leave_group_out(
    rows: Sequence[MomentumObservation], *, group: str,
    cost_bps: Decimal,
) -> dict[str, Any]:
    groups: dict[str, list[MomentumObservation]] = {}
    for row in rows:
        if group == "symbol":
            key = row.symbol
        elif group == "day":
            key = row.timestamp.date().isoformat()
        elif group == "week":
            iso = row.timestamp.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
        else:
            raise ValueError("group must be symbol, day or week")
        groups.setdefault(key, []).append(row)
    output = {}
    for omitted in sorted(groups):
        kept = [row.gross_strategy_bps for row in rows if row not in groups[omitted]]
        output[omitted] = economic_metrics(kept, cost_bps=cost_bps)
    return output


def stable_hash(value: Any) -> str:
    import json

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
