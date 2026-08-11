from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from typing import Any, Sequence

from app.microstructure.models import (
    BookLevel,
    CarryCandidate,
    CoverageState,
    HypotheticalQuote,
    HypotheticalTouchOutcome,
    LegSnapshot,
    SynchronizedSnapshot,
    TakerCostEstimate,
)
from app.v5.models import MarketLegSnapshot
from app.v5.research import build_carry_opportunity


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")
DEPTH_WINDOWS_BPS = (5, 10, 25, 50)
TOUCH_HORIZONS_SECONDS = (1, 5, 15, 30, 60)


def stable_id(*parts: object, length: int = 32) -> str:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def basis_bps(reference: Decimal, comparison: Decimal) -> Decimal:
    if reference <= 0 or comparison <= 0:
        raise ValueError("basis prices must be positive")
    return (comparison / reference - ONE) * BPS


def depth_windows(
    bids: Sequence[BookLevel], asks: Sequence[BookLevel], mid: Decimal,
) -> dict[str, dict[str, Decimal]]:
    if mid <= 0:
        raise ValueError("mid must be positive")
    output: dict[str, dict[str, Decimal]] = {}
    for window in DEPTH_WINDOWS_BPS:
        fraction = Decimal(window) / BPS
        bid_floor = mid * (ONE - fraction)
        ask_ceiling = mid * (ONE + fraction)
        output[str(window)] = {
            "bid": sum(
                (level.price * level.quantity for level in bids if level.price >= bid_floor),
                ZERO,
            ),
            "ask": sum(
                (level.price * level.quantity for level in asks if level.price <= ask_ceiling),
                ZERO,
            ),
        }
    return output


def build_leg_snapshot(
    *,
    category: str,
    symbol: str,
    exchange_timestamp: datetime,
    local_receive_timestamp: datetime,
    bids: Sequence[tuple[Decimal, Decimal]],
    asks: Sequence[tuple[Decimal, Decimal]],
    recent_trade_price: Decimal | None = None,
    recent_trade_volume: Decimal | None = None,
    recent_trade_timestamp: datetime | None = None,
    ticker: dict[str, Any] | None = None,
    funding_timestamp: datetime | None = None,
    funding_interval_minutes: int | None = None,
    open_interest: Decimal | None = None,
    open_interest_timestamp: datetime | None = None,
    open_interest_change_pct: Decimal | None = None,
    volatility_5m_bps: Decimal | None = None,
) -> LegSnapshot:
    clean_bids = sorted(
        (BookLevel(price=price, quantity=quantity) for price, quantity in bids if quantity > 0),
        key=lambda level: level.price,
        reverse=True,
    )
    clean_asks = sorted(
        (BookLevel(price=price, quantity=quantity) for price, quantity in asks if quantity > 0),
        key=lambda level: level.price,
    )
    if not clean_bids or not clean_asks:
        raise ValueError("both book sides are required")
    best_bid = clean_bids[0].price
    best_ask = clean_asks[0].price
    if best_bid >= best_ask:
        raise ValueError("book is crossed or locked")
    mid = (best_bid + best_ask) / Decimal("2")
    values = ticker or {}

    def optional_decimal(name: str) -> Decimal | None:
        raw = values.get(name)
        if raw in (None, ""):
            return None
        result = Decimal(str(raw))
        return result if result.is_finite() else None

    next_funding_raw = values.get("nextFundingTime")
    next_funding = (
        datetime.fromtimestamp(int(next_funding_raw) / 1000, tz=timezone.utc)
        if next_funding_raw not in (None, "", "0") else None
    )
    predicted = optional_decimal("predictedFundingRate")
    premium = optional_decimal("premiumIndex")
    return LegSnapshot(
        category=category,
        symbol=symbol,
        exchange_timestamp=exchange_timestamp,
        local_receive_timestamp=local_receive_timestamp,
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread_bps=(best_ask - best_bid) / mid * BPS,
        bids=clean_bids,
        asks=clean_asks,
        depth_bps_usdt=depth_windows(clean_bids, clean_asks, mid),
        recent_trade_price=recent_trade_price,
        recent_trade_volume=recent_trade_volume,
        recent_trade_timestamp=recent_trade_timestamp,
        mark_price=optional_decimal("markPrice") if category == "linear" else None,
        index_price=optional_decimal("indexPrice") if category == "linear" else None,
        current_funding_rate=(
            optional_decimal("fundingRate") if category == "linear" else None
        ),
        funding_timestamp=funding_timestamp if category == "linear" else None,
        predicted_funding_rate=predicted if category == "linear" else None,
        next_funding_time=next_funding if category == "linear" else None,
        funding_interval_minutes=(
            funding_interval_minutes if category == "linear" else None
        ),
        premium_index=premium if category == "linear" else None,
        open_interest=open_interest if category == "linear" else None,
        open_interest_timestamp=(
            open_interest_timestamp if category == "linear" else None
        ),
        open_interest_change_pct=(
            open_interest_change_pct if category == "linear" else None
        ),
        volatility_5m_bps=volatility_5m_bps,
    )


def synchronize_snapshot(
    *,
    symbol: str,
    spot: LegSnapshot | None,
    perpetual: LegSnapshot | None,
    completed_at: datetime,
    clock_offset_ms: Decimal | None,
    max_source_age_ms: Decimal,
    max_sync_gap_ms: Decimal,
) -> SynchronizedSnapshot:
    present = [leg for leg in (spot, perpetual) if leg is not None]
    exchange_timestamp = max(
        (leg.exchange_timestamp for leg in present), default=completed_at
    )
    received_at = max(
        (leg.local_receive_timestamp for leg in present), default=completed_at
    )
    spot_age = Decimal(str(max(
        0.0,
        (completed_at - spot.local_receive_timestamp).total_seconds() * 1000
        if spot is not None else float(max_source_age_ms + 1),
    )))
    perp_age = Decimal(str(max(
        0.0,
        (completed_at - perpetual.local_receive_timestamp).total_seconds() * 1000
        if perpetual is not None else float(max_source_age_ms + 1),
    )))
    sync_gap = Decimal(str(
        abs((spot.exchange_timestamp - perpetual.exchange_timestamp).total_seconds()) * 1000
        if spot is not None and perpetual is not None else float(max_sync_gap_ms + 1)
    ))
    funding_age = (
        Decimal(str(max(
            0.0,
            (
                completed_at - perpetual.funding_timestamp
                + timedelta(milliseconds=float(clock_offset_ms or ZERO))
            ).total_seconds() * 1000,
        )))
        if perpetual is not None and perpetual.funding_timestamp is not None else None
    )
    reasons: list[str] = []
    if spot is None:
        reasons.append("SPOT_BOOK_MISSING")
    if perpetual is None:
        reasons.append("PERP_BOOK_MISSING")
    if spot_age > max_source_age_ms:
        reasons.append("SPOT_BOOK_STALE")
    if perp_age > max_source_age_ms:
        reasons.append("PERP_BOOK_STALE")
    if sync_gap > max_sync_gap_ms:
        reasons.append("SPOT_PERP_SYNCHRONIZATION_GAP")
    if perpetual is not None:
        if perpetual.mark_price is None or perpetual.index_price is None:
            reasons.append("MARK_INDEX_MISSING")
        if perpetual.current_funding_rate is None:
            reasons.append("FUNDING_MISSING")
        if perpetual.next_funding_time is None or perpetual.funding_interval_minutes is None:
            reasons.append("FUNDING_SCHEDULE_MISSING")
        if perpetual.open_interest is None or perpetual.open_interest_timestamp is None:
            reasons.append("OPEN_INTEREST_MISSING")
    availability = {
        "spot_book": CoverageState.AVAILABLE if spot is not None else CoverageState.MISSING,
        "perp_book": (
            CoverageState.AVAILABLE if perpetual is not None else CoverageState.MISSING
        ),
        "mark_index": (
            CoverageState.AVAILABLE
            if perpetual is not None
            and perpetual.mark_price is not None
            and perpetual.index_price is not None
            else CoverageState.MISSING
        ),
        "funding": (
            CoverageState.AVAILABLE
            if perpetual is not None and perpetual.current_funding_rate is not None
            else CoverageState.MISSING
        ),
        "predicted_funding": (
            CoverageState.AVAILABLE
            if perpetual is not None and perpetual.predicted_funding_rate is not None
            else CoverageState.MISSING
        ),
        "open_interest": (
            CoverageState.AVAILABLE
            if perpetual is not None and perpetual.open_interest is not None
            else CoverageState.MISSING
        ),
    }
    capture_id = stable_id(
        "microstructure-capture", symbol,
        completed_at.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
    )
    return SynchronizedSnapshot(
        capture_id=capture_id,
        symbol=symbol,
        exchange_timestamp=exchange_timestamp,
        local_receive_timestamp=received_at,
        snapshot_completed_at=completed_at,
        spot_age_ms=spot_age,
        perp_age_ms=perp_age,
        funding_age_ms=funding_age,
        synchronization_gap_ms=sync_gap,
        clock_offset_ms=clock_offset_ms,
        spot=spot,
        perpetual=perpetual,
        perp_mid_vs_spot_mid_bps=(
            basis_bps(spot.mid, perpetual.mid)
            if spot is not None and perpetual is not None else None
        ),
        mark_vs_spot_bps=(
            basis_bps(spot.mid, perpetual.mark_price)
            if spot is not None and perpetual is not None
            and perpetual.mark_price is not None else None
        ),
        mark_vs_index_bps=(
            basis_bps(perpetual.index_price, perpetual.mark_price)
            if perpetual is not None and perpetual.mark_price is not None
            and perpetual.index_price is not None else None
        ),
        complete=not reasons,
        quality_reasons=reasons,
        availability=availability,
    )


def estimate_taker_cost(
    *,
    capture_id: str,
    symbol: str,
    venue_leg: str,
    side: str,
    notional_usdt: Decimal,
    leg: LegSnapshot,
    fee_bps: Decimal | None,
) -> TakerCostEstimate:
    normalized_side = side.upper()
    levels = leg.asks if normalized_side == "BUY" else leg.bids
    remaining = notional_usdt
    total_quantity = ZERO
    filled_notional = ZERO
    for level in levels:
        level_notional = level.price * level.quantity
        consumed = min(remaining, level_notional)
        if consumed <= 0:
            continue
        total_quantity += consumed / level.price
        filled_notional += consumed
        remaining -= consumed
        if remaining <= Decimal("0.00000001"):
            break
    sufficient = remaining <= Decimal("0.00000001") and total_quantity > 0
    vwap = filled_notional / total_quantity if sufficient else None
    slippage = None
    cross = None
    effective = None
    if vwap is not None:
        direction = ONE if normalized_side == "BUY" else Decimal("-1")
        slippage = direction * (vwap / leg.mid - ONE) * BPS
        top = leg.best_ask if normalized_side == "BUY" else leg.best_bid
        cross = direction * (top / leg.mid - ONE) * BPS
        effective = slippage + fee_bps if fee_bps is not None else None
    blockers = []
    if not sufficient:
        blockers.append("INSUFFICIENT_ORDERBOOK_DEPTH")
    if fee_bps is None:
        blockers.append("ACCOUNT_FEE_UNKNOWN")
    return TakerCostEstimate(
        cost_id=stable_id(
            "taker-cost", capture_id, venue_leg, normalized_side, notional_usdt
        ),
        capture_id=capture_id,
        symbol=symbol,
        venue_leg=venue_leg,
        side=normalized_side,
        notional_usdt=notional_usdt,
        sufficient_depth=sufficient,
        filled_notional_usdt=filled_notional,
        vwap=vwap,
        slippage_bps=slippage,
        spread_cross_bps=cross,
        fee_bps=fee_bps,
        estimated_effective_cost_bps=effective,
        blockers=blockers,
    )


def build_carry_candidate(
    snapshot: SynchronizedSnapshot,
    *,
    notionals: Sequence[Decimal],
    account_fees_bps: dict[str, Decimal | None],
    max_alignment_ms: Decimal = Decimal("2000"),
) -> tuple[CarryCandidate, list[TakerCostEstimate]]:
    spot = snapshot.spot
    perp = snapshot.perpetual
    all_costs: list[TakerCostEstimate] = []
    if spot is not None and perp is not None:
        for notional in notionals:
            all_costs.extend((
                estimate_taker_cost(
                    capture_id=snapshot.capture_id, symbol=snapshot.symbol,
                    venue_leg="spot", side="BUY", notional_usdt=notional,
                    leg=spot, fee_bps=account_fees_bps.get("spot_taker"),
                ),
                estimate_taker_cost(
                    capture_id=snapshot.capture_id, symbol=snapshot.symbol,
                    venue_leg="spot", side="SELL", notional_usdt=notional,
                    leg=spot, fee_bps=account_fees_bps.get("spot_taker"),
                ),
                estimate_taker_cost(
                    capture_id=snapshot.capture_id, symbol=snapshot.symbol,
                    venue_leg="perpetual", side="BUY", notional_usdt=notional,
                    leg=perp, fee_bps=account_fees_bps.get("perp_taker"),
                ),
                estimate_taker_cost(
                    capture_id=snapshot.capture_id, symbol=snapshot.symbol,
                    venue_leg="perpetual", side="SELL", notional_usdt=notional,
                    leg=perp, fee_bps=account_fees_bps.get("perp_taker"),
                ),
            ))
    current_rate = perp.current_funding_rate if perp is not None else None
    if current_rate is None:
        classification = "FUNDING_UNKNOWN"
        structure = None
    elif current_rate > 0:
        classification = "POSITIVE_FUNDING_CARRY"
        structure = "LONG_SPOT_SHORT_PERPETUAL"
    elif current_rate < 0:
        classification = "UNSUPPORTED_REVERSE_CARRY"
        structure = None
    else:
        classification = "ZERO_FUNDING_NO_CARRY"
        structure = None
    blockers = list(snapshot.quality_reasons)
    if any(value is None for value in account_fees_bps.values()):
        blockers.append("ACCOUNT_FEE_CONFIGURATION_UNKNOWN")
    if classification == "UNSUPPORTED_REVERSE_CARRY":
        blockers.append("SPOT_SHORT_BORROW_MECHANICS_UNAVAILABLE")

    notional_costs: dict[str, dict[str, Any]] = {}
    for notional in notionals:
        selected = {
            (row.venue_leg, row.side): row
            for row in all_costs if row.notional_usdt == notional
        }
        entry_rows = [selected.get(("spot", "BUY")), selected.get(("perpetual", "SELL"))]
        exit_rows = [selected.get(("spot", "SELL")), selected.get(("perpetual", "BUY"))]
        complete_cost = all(
            row is not None and row.estimated_effective_cost_bps is not None
            for row in entry_rows + exit_rows
        )
        entry_cost = (
            sum((row.estimated_effective_cost_bps for row in entry_rows if row), ZERO)
            if complete_cost else None
        )
        exit_cost = (
            sum((row.estimated_effective_cost_bps for row in exit_rows if row), ZERO)
            if complete_cost else None
        )
        round_trip = entry_cost + exit_cost if entry_cost is not None and exit_cost is not None else None
        funding_bps = current_rate * BPS if current_rate is not None else None
        break_even = (
            round_trip / funding_bps
            if round_trip is not None and funding_bps is not None and funding_bps > 0
            else None
        )
        notional_costs[str(notional)] = {
            "expected_funding_cashflow_usdt": (
                str(notional * current_rate) if current_rate is not None else None
            ),
            "entry_cost_bps": str(entry_cost) if entry_cost is not None else None,
            "exit_cost_bps": str(exit_cost) if exit_cost is not None else None,
            "full_round_trip_cost_bps": (
                str(round_trip) if round_trip is not None else None
            ),
            "break_even_funding_periods": str(break_even) if break_even is not None else None,
            "entry_legs": {
                "spot_buy": _cost_components(entry_rows[0]),
                "perpetual_sell": _cost_components(entry_rows[1]),
            },
            "exit_legs": {
                "spot_sell": _cost_components(exit_rows[0]),
                "perpetual_buy": _cost_components(exit_rows[1]),
            },
            "complete": complete_cost,
        }

    if spot is not None and perp is not None:
        spot_slippage = {
            str(row.notional_usdt): row.slippage_bps
            for row in all_costs if row.venue_leg == "spot" and row.side == "BUY"
        }
        perp_slippage = {
            str(row.notional_usdt): row.slippage_bps
            for row in all_costs if row.venue_leg == "perpetual" and row.side == "SELL"
        }
        canonical = build_carry_opportunity(
            symbol=snapshot.symbol,
            spot=MarketLegSnapshot(
                symbol=spot.symbol, category="spot",
                source_timestamp=spot.exchange_timestamp,
                received_at=max(spot.local_receive_timestamp, spot.exchange_timestamp),
                bid=spot.best_bid, ask=spot.best_ask,
                bid_depth_usdt=spot.depth_bps_usdt["50"]["bid"],
                ask_depth_usdt=spot.depth_bps_usdt["50"]["ask"],
                slippage_bps_by_notional=spot_slippage,
            ),
            perp=MarketLegSnapshot(
                symbol=perp.symbol, category="linear",
                source_timestamp=perp.exchange_timestamp,
                received_at=max(perp.local_receive_timestamp, perp.exchange_timestamp),
                bid=perp.best_bid, ask=perp.best_ask,
                mark_price=perp.mark_price, index_price=perp.index_price,
                bid_depth_usdt=perp.depth_bps_usdt["50"]["bid"],
                ask_depth_usdt=perp.depth_bps_usdt["50"]["ask"],
                slippage_bps_by_notional=perp_slippage,
            ),
            current_funding_rate=current_rate,
            predicted_funding_rate=perp.predicted_funding_rate,
            next_funding_time=perp.next_funding_time,
            funding_interval_hours=(
                Decimal(perp.funding_interval_minutes) / Decimal("60")
                if perp.funding_interval_minutes else None
            ),
            historical_funding={},
            account_fees_bps=account_fees_bps,
            max_alignment_ms=max_alignment_ms,
            source="MICROSTRUCTURE_SHADOW",
        )
        canonical_payload = canonical.model_dump(mode="json")
        opportunity_id = str(canonical.opportunity_id)
    else:
        canonical_payload = {
            "timestamp": snapshot.snapshot_completed_at.isoformat(),
            "blockers": snapshot.quality_reasons,
        }
        opportunity_id = stable_id("carry-opportunity", snapshot.capture_id)
    return CarryCandidate(
        opportunity_id=opportunity_id,
        capture_id=snapshot.capture_id,
        symbol=snapshot.symbol,
        timestamp=snapshot.snapshot_completed_at,
        classification=classification,
        structure=structure,
        funding_rate=current_rate,
        basis_bps=snapshot.perp_mid_vs_spot_mid_bps,
        notional_costs=notional_costs,
        canonical_opportunity=canonical_payload,
        blockers=list(dict.fromkeys(blockers)),
    ), all_costs


def _cost_components(row: TakerCostEstimate | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "spread_cross_bps": (
            str(row.spread_cross_bps) if row.spread_cross_bps is not None else None
        ),
        "slippage_bps": str(row.slippage_bps) if row.slippage_bps is not None else None,
        "fee_bps": str(row.fee_bps) if row.fee_bps is not None else "UNKNOWN",
        "estimated_effective_cost_bps": (
            str(row.estimated_effective_cost_bps)
            if row.estimated_effective_cost_bps is not None else None
        ),
        "vwap": str(row.vwap) if row.vwap is not None else None,
        "sufficient_depth": row.sufficient_depth,
    }


def hypothetical_quotes(snapshot: SynchronizedSnapshot) -> list[HypotheticalQuote]:
    rows: list[HypotheticalQuote] = []
    for venue_leg, leg in (("spot", snapshot.spot), ("perpetual", snapshot.perpetual)):
        if leg is None:
            continue
        for side, price in (("BUY", leg.best_bid), ("SELL", leg.best_ask)):
            rows.append(HypotheticalQuote(
                quote_id=stable_id(
                    "hypothetical-quote", snapshot.capture_id, venue_leg, side
                ),
                capture_id=snapshot.capture_id,
                symbol=snapshot.symbol,
                venue_leg=venue_leg,
                side=side,
                quote_price=price,
                quote_time=snapshot.snapshot_completed_at,
                best_bid=leg.best_bid,
                best_ask=leg.best_ask,
                spread_bps=leg.spread_bps,
            ))
    return rows


def evaluate_hypothetical_touch(
    quote: HypotheticalQuote,
    *,
    horizon_seconds: int,
    evaluated_at: datetime,
    trades: Sequence[tuple[datetime, Decimal]],
    midpoints: Sequence[tuple[datetime, Decimal]],
    endpoint_tolerance_seconds: int = 3,
) -> HypotheticalTouchOutcome:
    cutoff = quote.quote_time + timedelta(seconds=horizon_seconds)
    eligible = sorted(
        (
            (timestamp, price) for timestamp, price in trades
            if quote.quote_time < timestamp <= cutoff
        ),
        key=lambda item: item[0],
    )
    if quote.side == "BUY":
        touches = [row for row in eligible if row[1] <= quote.quote_price]
    else:
        touches = [row for row in eligible if row[1] >= quote.quote_price]
    touch = touches[0] if touches else None
    horizon_endpoint = next(
        (
            (timestamp, mid) for timestamp, mid in sorted(midpoints)
            if timestamp >= cutoff
            and (timestamp - cutoff).total_seconds() <= endpoint_tolerance_seconds
        ),
        None,
    )
    complete = evaluated_at >= cutoff and horizon_endpoint is not None
    markout = None
    if touch is not None:
        markout_target = touch[0] + timedelta(seconds=horizon_seconds)
        endpoint = next(
            (
                (timestamp, mid) for timestamp, mid in sorted(midpoints)
                if timestamp >= markout_target
                and (timestamp - markout_target).total_seconds() <= endpoint_tolerance_seconds
            ),
            None,
        )
        if endpoint is not None:
            direction = ONE if quote.side == "BUY" else Decimal("-1")
            markout = direction * (endpoint[1] / quote.quote_price - ONE) * BPS
        complete = evaluated_at >= markout_target and endpoint is not None
    return HypotheticalTouchOutcome(
        quote_id=quote.quote_id,
        horizon_seconds=horizon_seconds,
        evaluated_at=evaluated_at,
        would_touch=touch is not None,
        estimated_time_to_touch_seconds=(
            Decimal(str((touch[0] - quote.quote_time).total_seconds()))
            if touch is not None else None
        ),
        touch_time=touch[0] if touch is not None else None,
        markout_bps=markout,
        complete=complete,
    )
