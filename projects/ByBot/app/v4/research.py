from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import math
from typing import Any, Iterable, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from app.v2.models import MarketFeatureSnapshot
from app.v4.models import (
    FeatureAvailability,
    FeatureTiming,
    V4Decision,
    V4ForwardLabel,
    V4Opportunity,
    V4RejectionReason,
)


ZERO = Decimal("0")
HORIZONS_SECONDS = (15, 30, 60, 120, 300, 600, 900)
COST_STRESS_BPS = (8, 11, 13, 15, 18, 20)
DEFAULT_BARRIERS_BPS = (
    (10, 10), (15, 10), (20, 10), (20, 15),
    (30, 15), (30, 20), (40, 20), (50, 25),
)
V4_FEATURE_SCHEMA_VERSION = "4.0.0"
V4_DETECTOR_VERSION = "4.0.0"


def _d(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ArithmeticError, ValueError):
        return None


def _value(mapping: dict[str, Decimal], key: str) -> Decimal | None:
    return _d(mapping.get(key))


def directional_return_bps(
    entry_price: Decimal, future_price: Decimal, side: str,
) -> Decimal:
    direction = Decimal("1") if side.upper() == "BUY" else Decimal("-1")
    return direction * (future_price / entry_price - Decimal("1")) * Decimal("10000")


def deterministic_cycle_id(
    *, run_id: str, symbol: str, snapshot_time: datetime,
) -> str:
    value = uuid5(
        NAMESPACE_URL,
        f"bybot-v4-cycle|{run_id}|{symbol}|{snapshot_time.astimezone(timezone.utc).isoformat()}",
    )
    return str(value)


def deterministic_opportunity_id(
    *, run_id: str, cycle_id: str, symbol: str, side: str,
    snapshot_time: datetime, candidate_type: str = "V4_VOLATILITY_EXPANSION",
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        "|".join((
            "bybot-v4-opportunity", V4_DETECTOR_VERSION, run_id, cycle_id,
            symbol, side, candidate_type,
            snapshot_time.astimezone(timezone.utc).isoformat(),
        )),
    )


def _ratio(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    if left is None or right is None or right == 0:
        return None
    return left / right


def _sign(value: Decimal | None) -> int:
    return 1 if value is not None and value > 0 else -1 if value is not None and value < 0 else 0


def _availability(value: Any) -> FeatureAvailability:
    return (
        FeatureAvailability.PRE_ENTRY_AVAILABLE
        if value is not None else FeatureAvailability.UNKNOWN
    )


def feature_vector(snapshot: MarketFeatureSnapshot, *, side: str) -> dict[str, Any]:
    """Map a real V2 snapshot to V4 pre-entry features without imputation."""
    direction = Decimal("1") if side == "BUY" else Decimal("-1")
    momentum = {key: _value(snapshot.price_momentum, key) for key in (
        "10s", "30s", "1m", "3m", "5m", "15m",
    )}
    volatility = {key: _value(snapshot.realized_volatility, key) for key in (
        "10s", "30s", "1m", "3m", "5m", "15m", "1h",
    )}
    volume = {key: _value(snapshot.volume_acceleration, key) for key in (
        "30s", "1m", "3m", "5m", "15m",
    )}
    trade_flow = {key: _value(snapshot.trade_imbalance, key) for key in (
        "30s", "1m", "3m", "5m",
    )}
    order_flow = {key: _value(snapshot.order_flow_imbalance, key) for key in (
        "30s", "1m", "3m", "5m",
    )}
    breakout_1m = _value(snapshot.breakout_distance_bps, "1m")
    high_distance = _d(snapshot.distance_from_high_bps)
    low_distance = _d(snapshot.distance_from_low_bps)
    range_width = (
        abs(high_distance) + abs(low_distance)
        if high_distance is not None and low_distance is not None else None
    )
    range_position = (
        abs(low_distance) / range_width
        if range_width is not None and range_width > 0 and low_distance is not None
        else None
    )
    signed_momentum = direction * (momentum["1m"] or ZERO)
    oi_change = _d(snapshot.open_interest_change_pct)
    price_oi_relationship = (
        "PRICE_UP_OI_UP" if _sign(momentum["5m"]) >= 0 and _sign(oi_change) >= 0
        else "PRICE_UP_OI_DOWN" if _sign(momentum["5m"]) >= 0
        else "PRICE_DOWN_OI_UP" if _sign(oi_change) >= 0
        else "PRICE_DOWN_OI_DOWN"
    ) if oi_change is not None and momentum["5m"] is not None else None
    book_total = snapshot.bid_depth_usdt + snapshot.ask_depth_usdt
    executable_total = snapshot.bid_depth_10bps_usdt + snapshot.ask_depth_10bps_usdt
    estimated_slippage = (
        snapshot.spread_bps / Decimal("2")
        + (Decimal("1") / max(executable_total, Decimal("1"))) * Decimal("10000")
    )
    values: dict[str, Any] = {
        # Exact 5s/15s/120s returns are not present in the historical V2 snapshot.
        "return_5s_bps": None,
        "return_15s_bps": None,
        "return_30s_bps": momentum["30s"],
        "return_60s_bps": momentum["1m"],
        "return_120s_bps": None,
        "return_300s_bps": momentum["5m"],
        "momentum_1m_bps": momentum["1m"],
        "momentum_5m_bps": momentum["5m"],
        "momentum_acceleration_bps": (
            momentum["1m"] - momentum["3m"]
            if momentum["1m"] is not None and momentum["3m"] is not None else None
        ),
        "directional_momentum_1m_bps": signed_momentum,
        "distance_from_local_high_bps": high_distance,
        "distance_from_local_low_bps": low_distance,
        "range_position": range_position,
        "range_width_bps": range_width,
        "breakout_distance_1m_bps": breakout_1m,
        "directional_breakout_distance_bps": direction * (breakout_1m or ZERO),
        "realized_volatility_1m": volatility["1m"],
        "realized_volatility_5m": volatility["5m"],
        "realized_volatility_15m": volatility["15m"],
        "atr_bps": _d(snapshot.atr_bps),
        "range_compression_ratio": _ratio(volatility["1m"], volatility["15m"]),
        "short_long_volatility_ratio": _ratio(volatility["1m"], volatility["15m"]),
        "volatility_percentile": None,
        "time_inside_recent_range_seconds": None,
        "volume_24h": _d(snapshot.volume_24h),
        "volume_acceleration_1m": volume["1m"],
        "volume_acceleration_5m": volume["5m"],
        "volume_z_score": None,
        "relative_volume": volume["1m"],
        "trade_count_1m": snapshot.observation_count.get("1m"),
        "aggressive_buy_volume": None,
        "aggressive_sell_volume": None,
        "trade_imbalance_1m": trade_flow["1m"],
        "signed_volume_imbalance": trade_flow["1m"],
        "best_bid": snapshot.bid_price,
        "best_ask": snapshot.ask_price,
        "spread_bps": snapshot.spread_bps,
        "bid_depth_usdt": snapshot.bid_depth_usdt,
        "ask_depth_usdt": snapshot.ask_depth_usdt,
        "bid_depth_10bps_usdt": snapshot.bid_depth_10bps_usdt,
        "ask_depth_10bps_usdt": snapshot.ask_depth_10bps_usdt,
        "orderbook_imbalance": _d(snapshot.orderbook_imbalance),
        "microprice": _d(snapshot.microprice),
        "microprice_deviation_bps": _d(snapshot.microprice_deviation_bps),
        "book_pressure": _d(snapshot.orderbook_imbalance),
        "order_flow_imbalance_1m": order_flow["1m"],
        "estimated_market_slippage_bps": estimated_slippage,
        "book_fresh": snapshot.source_age_seconds.get("orderbook") is not None,
        "open_interest": _d(snapshot.open_interest),
        "open_interest_change_5m_pct": oi_change,
        "open_interest_velocity": None,
        "funding_rate": _d(snapshot.funding_rate),
        "funding_delta": None,
        "funding_extremeness_bps": _d(snapshot.funding_deviation_bps),
        "price_oi_relationship": price_oi_relationship,
        "liquidation_long_usdt_5m": _d(snapshot.liquidation_long_usdt),
        "liquidation_short_usdt_5m": _d(snapshot.liquidation_short_usdt),
        "liquidation_imbalance": _d(snapshot.liquidation_imbalance),
        "liquidation_notional_5m": _d(snapshot.liquidation_notional_5m),
        "liquidation_event_count_5m": snapshot.liquidation_event_count_5m,
        "time_since_liquidation_seconds": snapshot.liquidation_data_age_seconds,
        "price_move_since_liquidation_bps": None,
        "btc_relative_strength_bps": _d(snapshot.relative_strength_vs_btc_bps),
        "btc_correlation": _d(snapshot.rolling_correlation_vs_btc),
        "btc_beta": _d(snapshot.btc_beta),
        "eth_context": None,
        "market_breadth": None,
        "cross_symbol_dispersion": None,
        "market_regime": snapshot.market_regime,
        "trend_range_regime": snapshot.market_regime,
        "candidate_rank": None,
        "competing_candidates": None,
        "current_positions": None,
        "current_portfolio_exposure": None,
        "correlation_group": None,
        "price_at_event": None,
        "price_at_first_seen": snapshot.last_price,
        "price_at_candidate": snapshot.last_price,
        "price_at_decision": snapshot.last_price,
        "already_moved_event_to_seen_bps": None,
        "already_moved_event_to_candidate_bps": None,
        "already_moved_seen_to_candidate_bps": ZERO,
        "breakout_distance_at_detection_bps": breakout_1m,
        "breakout_distance_at_decision_bps": breakout_1m,
        "distance_from_range_boundary_bps": (
            abs(high_distance) if side == "BUY" and high_distance is not None
            else abs(low_distance) if low_distance is not None else None
        ),
        "feature_schema_version": V4_FEATURE_SCHEMA_VERSION,
        "executable_depth_total_usdt": executable_total,
        "book_depth_total_usdt": book_total,
    }
    return values


def feature_availability(features: dict[str, Any]) -> dict[str, FeatureAvailability]:
    return {name: _availability(value) for name, value in features.items()}


def feature_timing(snapshot: MarketFeatureSnapshot) -> dict[str, FeatureTiming]:
    groups = {
        "price": "ticker", "momentum": "trades", "volatility": "trades",
        "volume": "trades", "trade_flow": "trades", "order_flow": "trades",
        "orderbook": "orderbook", "open_interest": "open_interest",
        "funding": "funding", "liquidations": "liquidations",
        "regime": "trades", "portfolio": "runtime",
    }
    output: dict[str, FeatureTiming] = {}
    for group, source in groups.items():
        source_at = snapshot.source_timestamps.get(source)
        age = snapshot.source_age_seconds.get(source)
        available = source_at is not None and age is not None
        output[group] = FeatureTiming(
            source=source,
            source_timestamp=source_at,
            local_receive_timestamp=snapshot.timestamp,
            age_ms=Decimal(str(age * 1000)) if age is not None else None,
            freshness_limit_ms=Decimal("15000") if source in {
                "ticker", "trades", "orderbook",
            } else Decimal("180000"),
            availability=(
                FeatureAvailability.PRE_ENTRY_AVAILABLE
                if available else FeatureAvailability.UNKNOWN
            ),
        )
    return output


def candidate_layers(features: dict[str, Any], *, side: str, fresh: bool) -> dict[str, bool]:
    direction = Decimal("1") if side == "BUY" else Decimal("-1")
    momentum = _d(features.get("momentum_1m_bps")) or ZERO
    breakout = _d(features.get("breakout_distance_1m_bps")) or ZERO
    atr = _d(features.get("atr_bps")) or ZERO
    vol_ratio = _d(features.get("short_long_volatility_ratio"))
    volume = _d(features.get("volume_acceleration_1m"))
    trade_flow = _d(features.get("trade_imbalance_1m"))
    order_flow = _d(features.get("order_flow_imbalance_1m"))
    regime = str(features.get("market_regime") or "UNKNOWN")
    oi = _d(features.get("open_interest_change_5m_pct"))
    book_flow = _d(features.get("orderbook_imbalance"))
    spread = _d(features.get("spread_bps"))
    depth = _d(features.get("executable_depth_total_usdt"))
    estimated_cost = (_d(features.get("estimated_market_slippage_bps")) or ZERO) + Decimal("8")
    expected_move_proxy = max(atr, abs(_d(features.get("momentum_5m_bps")) or ZERO))

    a = bool(
        fresh and vol_ratio is not None and vol_ratio >= Decimal("1.05")
        and (
            direction * breakout >= Decimal("0.25")
            or direction * momentum >= max(Decimal("4"), atr / Decimal("4"))
        )
    )
    b = bool(a and volume is not None and volume >= Decimal("1.10")
             and trade_flow is not None and direction * trade_flow >= ZERO)
    c = bool(b and regime not in ({"TRENDING_DOWN"} if side == "BUY" else {"TRENDING_UP"}))
    # Positioning is secondary: NO_INFORMATION is neutral, contradiction is not.
    positioning_ok = oi is None or direction * oi >= ZERO
    d = bool(c and positioning_ok)
    flow_ok = (
        (order_flow is None or direction * order_flow >= ZERO)
        and (book_flow is None or direction * book_flow >= Decimal("-0.20"))
    )
    e = bool(d and flow_ok and spread is not None and spread <= Decimal("15")
             and depth is not None and depth > ZERO)
    already_moved = abs(momentum)
    f = bool(e and already_moved <= max(Decimal("20"), atr * Decimal("2.5")))
    g = bool(f and expected_move_proxy > estimated_cost + Decimal("4"))
    return {
        "A_BREAKOUT_ONLY": a,
        "B_BREAKOUT_PLUS_VOLUME": b,
        "C_BREAKOUT_PLUS_REGIME": c,
        "D_BREAKOUT_PLUS_OI_CONFIRMATION": d,
        "E_BREAKOUT_PLUS_ORDER_FLOW_LIQUIDITY": e,
        "F_BREAKOUT_PLUS_LATE_ENTRY": f,
        "G_FULL_COST_AWARE": g,
    }


def rejection_reasons(
    snapshot: MarketFeatureSnapshot, features: dict[str, Any], layers: dict[str, bool],
) -> list[V4RejectionReason]:
    result: list[V4RejectionReason] = []
    if not snapshot.fresh:
        result.append(V4RejectionReason.DATA_STALE)
    if features.get("short_long_volatility_ratio") is None:
        result.append(V4RejectionReason.FEATURES_INCOMPLETE)
    if not layers["A_BREAKOUT_ONLY"]:
        result.extend((
            V4RejectionReason.NO_VOLATILITY_EXPANSION,
            V4RejectionReason.BREAKOUT_NOT_CONFIRMED,
        ))
    if layers["A_BREAKOUT_ONLY"] and not layers["B_BREAKOUT_PLUS_VOLUME"]:
        result.append(V4RejectionReason.VOLUME_CONFIRMATION_FAILED)
    if layers["B_BREAKOUT_PLUS_VOLUME"] and not layers["C_BREAKOUT_PLUS_REGIME"]:
        result.append(V4RejectionReason.REGIME_CONTRADICTS)
    if layers["C_BREAKOUT_PLUS_REGIME"] and not layers["D_BREAKOUT_PLUS_OI_CONFIRMATION"]:
        result.append(V4RejectionReason.OI_CONFIRMATION_FAILED)
    if layers["D_BREAKOUT_PLUS_OI_CONFIRMATION"] and not layers["E_BREAKOUT_PLUS_ORDER_FLOW_LIQUIDITY"]:
        spread = _d(features.get("spread_bps"))
        depth = _d(features.get("executable_depth_total_usdt"))
        if spread is not None and spread > Decimal("15"):
            result.append(V4RejectionReason.SPREAD_TOO_WIDE)
        if depth is None or depth <= 0:
            result.append(V4RejectionReason.LIQUIDITY_INSUFFICIENT)
        result.append(V4RejectionReason.ORDER_FLOW_CONTRADICTS)
    if layers["E_BREAKOUT_PLUS_ORDER_FLOW_LIQUIDITY"] and not layers["F_BREAKOUT_PLUS_LATE_ENTRY"]:
        result.append(V4RejectionReason.ALREADY_MOVED_TOO_FAR)
    if layers["F_BREAKOUT_PLUS_LATE_ENTRY"] and not layers["G_FULL_COST_AWARE"]:
        result.append(V4RejectionReason.EXPECTED_MOVE_BELOW_COST)
    if not layers["G_FULL_COST_AWARE"]:
        result.append(V4RejectionReason.META_MODEL_NO_TRADE)
    return list(dict.fromkeys(result))


def build_opportunity(
    snapshot: MarketFeatureSnapshot, *, run_id: str, cycle_id: str | None = None,
    source: str = "V4_SHADOW_RUNTIME",
) -> V4Opportunity:
    momentum = _value(snapshot.price_momentum, "1m") or _value(snapshot.price_momentum, "30s") or ZERO
    side = "BUY" if momentum >= 0 else "SELL"
    actual_cycle_id = cycle_id or deterministic_cycle_id(
        run_id=run_id, symbol=snapshot.symbol.value, snapshot_time=snapshot.timestamp,
    )
    features = feature_vector(snapshot, side=side)
    layers = candidate_layers(features, side=side, fresh=snapshot.fresh)
    reasons = rejection_reasons(snapshot, features, layers)
    decision = V4Decision.SHADOW_TRADE if layers["G_FULL_COST_AWARE"] else V4Decision.NO_TRADE
    timestamp = snapshot.timestamp
    return V4Opportunity(
        opportunity_id=deterministic_opportunity_id(
            run_id=run_id, cycle_id=actual_cycle_id, symbol=snapshot.symbol.value,
            side=side, snapshot_time=timestamp,
        ),
        cycle_id=actual_cycle_id,
        run_id=run_id,
        symbol=snapshot.symbol.value,
        side=side,
        source=source,
        event_time=None,
        first_seen_time=timestamp,
        feature_snapshot_time=timestamp,
        candidate_time=timestamp,
        decision_time=timestamp,
        order_possible_time=timestamp,
        entry_reference_price=snapshot.last_price,
        features=features,
        feature_timing=feature_timing(snapshot),
        availability=feature_availability(features),
        decision=decision,
        rejection_reasons=reasons,
        candidate_layers=layers,
    )


def cost_components(
    *, maker_taker_fees_bps: Decimal, spread_bps: Decimal,
    estimated_slippage_bps: Decimal, funding_bps: Decimal = ZERO,
    other_execution_costs_bps: Decimal = ZERO,
) -> dict[str, Decimal]:
    values = {
        "maker_taker_fees_bps": maker_taker_fees_bps,
        "spread_bps": spread_bps,
        "estimated_slippage_bps": estimated_slippage_bps,
        "funding_bps": funding_bps,
        "other_execution_costs_bps": other_execution_costs_bps,
    }
    if any(value < 0 for value in values.values()):
        raise ValueError("cost components cannot be negative")
    values["modeled_total_bps"] = sum(values.values(), ZERO)
    return values


def _first_barrier(
    returns: Sequence[tuple[datetime, Decimal]], positive: Decimal, adverse: Decimal,
) -> str:
    for _, value in returns:
        if value >= positive:
            return "TARGET"
        if value <= -adverse:
            return "ADVERSE"
    return "NEITHER"


def build_forward_label(
    opportunity: V4Opportunity,
    path: Sequence[tuple[datetime, Decimal]],
    *,
    generated_at: datetime | None = None,
    horizons: Sequence[int] = HORIZONS_SECONDS,
    barriers: Sequence[tuple[int, int]] = DEFAULT_BARRIERS_BPS,
    components: dict[str, Decimal] | None = None,
    maximum_endpoint_gap_seconds: int = 30,
) -> V4ForwardLabel:
    points = sorted(
        (
            (timestamp, price)
            for timestamp, price in path
            if timestamp > opportunity.decision_time and price > 0
        ),
        key=lambda item: item[0],
    )
    costs = components or cost_components(
        maker_taker_fees_bps=Decimal("8"),
        spread_bps=_d(opportunity.features.get("spread_bps")) or ZERO,
        estimated_slippage_bps=_d(
            opportunity.features.get("estimated_market_slippage_bps")
        ) or ZERO,
    )
    labels: dict[str, Any] = {}
    maximum_horizon = max(horizons)
    for horizon in horizons:
        cutoff = opportunity.decision_time + timedelta(seconds=horizon)
        eligible = [(ts, price) for ts, price in points if ts <= cutoff]
        endpoint = eligible[-1] if eligible else None
        endpoint_fresh = bool(
            endpoint and (cutoff - endpoint[0]).total_seconds() <= maximum_endpoint_gap_seconds
        )
        returns = [
            (timestamp, directional_return_bps(
                opportunity.entry_reference_price, price, opportunity.side,
            ))
            for timestamp, price in eligible
        ]
        suffix = f"{horizon}s"
        labels[f"coverage_{suffix}"] = "OBSERVED" if endpoint_fresh else "UNKNOWN"
        labels[f"gross_forward_bps_{suffix}"] = str(returns[-1][1]) if endpoint_fresh else None
        labels[f"mfe_bps_{suffix}"] = str(max([ZERO, *(row[1] for row in returns)])) if returns else None
        labels[f"mae_bps_{suffix}"] = str(min([ZERO, *(row[1] for row in returns)])) if returns else None
        if returns:
            mfe_row = max(returns, key=lambda row: row[1])
            mae_row = min(returns, key=lambda row: row[1])
            labels[f"time_to_mfe_seconds_{suffix}"] = (
                (mfe_row[0] - opportunity.decision_time).total_seconds()
                if mfe_row[1] > ZERO else 0.0
            )
            labels[f"time_to_mae_seconds_{suffix}"] = (
                (mae_row[0] - opportunity.decision_time).total_seconds()
                if mae_row[1] < ZERO else 0.0
            )
        else:
            labels[f"time_to_mfe_seconds_{suffix}"] = None
            labels[f"time_to_mae_seconds_{suffix}"] = None
    all_returns = [
        (timestamp, directional_return_bps(
            opportunity.entry_reference_price, price, opportunity.side,
        ))
        for timestamp, price in points
        if timestamp <= opportunity.decision_time + timedelta(seconds=maximum_horizon)
    ]
    for positive, adverse in barriers:
        labels[f"barrier_plus_{positive}_before_minus_{adverse}"] = _first_barrier(
            all_returns, Decimal(positive), Decimal(adverse)
        )
    primary = labels.get("gross_forward_bps_300s")
    labels["gross_forward_bps"] = primary
    for stress in COST_STRESS_BPS:
        labels[f"net_forward_bps_cost_{stress}"] = (
            str(Decimal(primary) - Decimal(stress)) if primary is not None else None
        )
    matured_at = opportunity.decision_time + timedelta(seconds=maximum_horizon)
    now = generated_at or datetime.now(timezone.utc)
    return V4ForwardLabel(
        opportunity_id=opportunity.opportunity_id,
        symbol=opportunity.symbol,
        side=opportunity.side,
        decision_time=opportunity.decision_time,
        label_generated_at=now,
        maximum_horizon_seconds=maximum_horizon,
        observation_count=len(all_returns),
        labels=labels,
        cost_components_bps=costs,
        complete=now >= matured_at,
    )


def chronological_splits(
    opportunities: Sequence[V4Opportunity], *, folds: int = 4,
    holdout_fraction: Decimal = Decimal("0.20"),
    label_horizon_seconds: int = 900,
) -> dict[str, Any]:
    ordered = sorted(opportunities, key=lambda row: (row.decision_time, str(row.opportunity_id)))
    if not ordered:
        return {
            "method": "expanding_chronological_with_label_purge",
            "holdout_frozen": True,
            "development_ids": [], "holdout_ids": [], "folds": [],
        }
    holdout_count = max(1, int(Decimal(len(ordered)) * holdout_fraction))
    development = ordered[:-holdout_count]
    holdout = ordered[-holdout_count:]
    segment = max(1, len(development) // (folds + 1))
    output_folds: list[dict[str, Any]] = []
    for number in range(1, folds + 1):
        start = number * segment
        end = len(development) if number == folds else min(len(development), (number + 1) * segment)
        validation = development[start:end]
        if not validation:
            continue
        cutoff = validation[0].decision_time - timedelta(seconds=label_horizon_seconds)
        train = [row for row in development[:start] if row.decision_time < cutoff]
        output_folds.append({
            "fold": number,
            "train_ids": [str(row.opportunity_id) for row in train],
            "validation_ids": [str(row.opportunity_id) for row in validation],
            "train_end": train[-1].decision_time.isoformat() if train else None,
            "validation_start": validation[0].decision_time.isoformat(),
            "preprocessing_fit_scope": "TRAIN_ONLY",
        })
    return {
        "method": "expanding_chronological_with_900s_label_purge",
        "holdout_frozen": True,
        "holdout_used_for_selection": False,
        "development_ids": [str(row.opportunity_id) for row in development],
        "holdout_ids": [str(row.opportunity_id) for row in holdout],
        "holdout_start": holdout[0].decision_time.isoformat(),
        "folds": output_folds,
    }


def economic_metrics(values: Sequence[Decimal], *, cost_bps: Decimal) -> dict[str, Any]:
    net = [value - cost_bps for value in values]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value < 0]
    equity = ZERO
    peak = ZERO
    drawdown = ZERO
    for value in net:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "observation_count": len(net),
        "gross_mean_bps": str(sum(values, ZERO) / Decimal(len(values))) if values else None,
        "net_expectancy_bps": str(sum(net, ZERO) / Decimal(len(net))) if net else None,
        "hit_rate": str(Decimal(len(wins)) / Decimal(len(net))) if net else None,
        "profit_factor": str(sum(wins, ZERO) / abs(sum(losses, ZERO))) if losses else None,
        "maximum_drawdown_bps": str(drawdown),
        "cost_bps": str(cost_bps),
    }


def stable_payload_hash(value: Any) -> str:
    import json

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
