from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.config import Settings
from app.models import DemoExecutionRecord, DemoExecutionState, Side, Symbol
from app.v2.analytics import _signal_row, _trade_row
from app.v2.execution import calculate_v2_position_sizing
from app.v2.models import (
    MarketFeatureSnapshot,
    PortfolioReservation,
    ReservationState,
    ScoreComponents,
    StrategyName,
    StrategySide,
    UniverseInstrument,
    V2SignalCandidate,
)
from app.v2.portfolio import (
    PortfolioRiskService,
    normalize_order_quantity,
    normalize_sized_order_quantity,
)


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "v2_enabled": True,
        "allowed_symbols": tuple(symbol.value for symbol in Symbol),
        "v2_min_expected_edge_bps": Decimal("3"),
        "v2_max_book_participation_pct": Decimal("5"),
        "v2_global_entry_cooldown_seconds": 0,
        "v2_symbol_cooldown_seconds": 0,
    }
    values.update(updates)
    return Settings(**values)


def _feature(
    symbol: Symbol = Symbol.BTCUSDT,
    *,
    spread_bps: Decimal = Decimal("2"),
    depth: Decimal = Decimal("10000"),
) -> MarketFeatureSnapshot:
    return MarketFeatureSnapshot(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        fresh=True,
        last_price=Decimal("100"),
        bid_price=Decimal("99.99"),
        ask_price=Decimal("100.01"),
        spread_bps=spread_bps,
        bid_depth_usdt=depth,
        ask_depth_usdt=depth,
        bid_depth_10bps_usdt=depth,
        ask_depth_10bps_usdt=depth,
        market_regime="TRENDING_UP",
    )


def _components(score: Decimal) -> ScoreComponents:
    return ScoreComponents(
        strategy_score=score,
        liquidity_score=score,
        market_confirmation_score=score,
        relative_strength_score=score,
        estimated_fee_penalty=Decimal("0"),
        estimated_slippage_penalty=Decimal("0"),
        correlation_penalty=Decimal("0"),
        portfolio_exposure_penalty=Decimal("0"),
        final_score=score,
    )


def _candidate(
    *,
    score: Decimal = Decimal("0.81"),
    symbol: Symbol = Symbol.BTCUSDT,
    gross_edge: Decimal = Decimal("80"),
    fees: Decimal = Decimal("12"),
    slippage: Decimal = Decimal("3"),
    funding: Decimal = Decimal("0"),
    stop: Decimal = Decimal("0.5"),
    depth: Decimal = Decimal("10000"),
) -> V2SignalCandidate:
    now = datetime.now(timezone.utc)
    return V2SignalCandidate(
        run_id="sizing-test",
        strategy_name=StrategyName.VOLUME_BREAKOUT,
        strategy_version="test",
        symbol=symbol,
        side=StrategySide.LONG,
        created_at=now,
        expires_at=now + timedelta(minutes=1),
        market_regime="TRENDING_UP",
        feature_snapshot=_feature(symbol, depth=depth),
        raw_strategy_score=score,
        confidence=score,
        estimated_edge_bps=gross_edge,
        expected_fees_bps=fees,
        expected_slippage_bps=slippage,
        expected_funding_bps=funding,
        entry_reason="test",
        threshold=Decimal("0.62"),
        distance_to_threshold=score - Decimal("0.62"),
        score_components=_components(score),
        admitted=True,
        state="READY",
        stop_loss_pct=stop,
        take_profit_pct=stop * Decimal("2"),
        maximum_holding_seconds=300,
    )


def _size(
    candidate: V2SignalCandidate,
    *,
    settings: Settings | None = None,
    active: Decimal = Decimal("0"),
) -> object:
    return calculate_v2_position_sizing(
        settings or _settings(),
        candidate=candidate,
        account_round_trip_fee_bps=candidate.expected_fees_bps,
        stop_loss_pct=candidate.stop_loss_pct,
        executable_depth_usdt=candidate.feature_snapshot.ask_depth_10bps_usdt,
        active_notional_usdt=active,
    )


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("0.62", "100"),
        ("0.66", "150"),
        ("0.70", "200"),
        ("0.75", "250"),
        ("0.81", "300"),
    ],
)
def test_all_confidence_tiers(score: str, expected: str) -> None:
    decision = _size(_candidate(score=Decimal(score)))
    assert decision.confidence_cap_usdt == Decimal(expected)
    assert decision.requested_notional_usdt == Decimal(expected)


@pytest.mark.parametrize(
    ("net_edge", "expected"),
    [("4", "100"), ("8", "150"), ("15", "200"), ("25", "300")],
)
def test_all_net_edge_tiers(net_edge: str, expected: str) -> None:
    candidate = _candidate(gross_edge=Decimal(net_edge) + Decimal("15"))
    decision = _size(candidate, settings=_settings(v2_min_expected_edge_bps=2))
    assert decision.expected_net_edge_bps == Decimal(net_edge)
    assert decision.edge_cap_usdt == Decimal(expected)


def test_risk_by_stop_and_wide_stop_reduce_size() -> None:
    normal = _size(_candidate(stop=Decimal("0.5")))
    wide = _size(_candidate(stop=Decimal("5")))
    assert normal.risk_budget_usdt == Decimal("5")
    assert normal.risk_cap_usdt == Decimal("1000")
    assert wide.risk_cap_usdt == Decimal("100")
    assert wide.requested_notional_usdt == Decimal("100")


def test_leverage_never_increases_economic_risk() -> None:
    candidate = _candidate(stop=Decimal("2"))
    low = _size(candidate, settings=_settings(core_leverage=Decimal("1")))
    high = _size(candidate, settings=_settings(core_leverage=Decimal("10")))
    assert low.requested_notional_usdt == high.requested_notional_usdt
    assert low.risk_cap_usdt == high.risk_cap_usdt == Decimal("250")


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        (Symbol.BTCUSDT, "300"),
        (Symbol.LINKUSDT, "250"),
        (Symbol.WIFUSDT, "150"),
    ],
)
def test_symbol_category_caps(symbol: Symbol, expected: str) -> None:
    decision = _size(_candidate(symbol=symbol))
    assert decision.symbol_cap_usdt == Decimal(expected)
    assert decision.requested_notional_usdt == Decimal(expected)


def test_total_notional_and_correlation_caps() -> None:
    decision = _size(_candidate(), active=Decimal("650"))
    assert decision.portfolio_remaining_capacity_usdt == Decimal("100")
    assert decision.requested_notional_usdt == Decimal("100")
    portfolio = PortfolioRiskService(_settings())
    for symbol in (Symbol.BTCUSDT, Symbol.ETHUSDT):
        assert portfolio.reserve(
            run_id="r",
            candidate_id=uuid4(),
            symbol=symbol,
            strategy_name=StrategyName.VOLUME_BREAKOUT,
            notional=Decimal("100"),
            risk_usdt=Decimal("0.5"),
        )
    assert "maximum positions for correlation group reached" in (
        portfolio.block_reasons(Symbol.BTCUSDT, Decimal("100"))
    )


def test_executable_depth_cap() -> None:
    decision = _size(_candidate(depth=Decimal("3000")))
    assert decision.liquidity_cap_usdt == Decimal("150")
    assert decision.requested_notional_usdt == Decimal("150")


def _instrument(
    *,
    step: Decimal = Decimal("1"),
    minimum: Decimal = Decimal("1"),
) -> UniverseInstrument:
    return UniverseInstrument(
        symbol=Symbol.XRPUSDT,
        status="Trading",
        min_order_qty=minimum,
        qty_step=step,
        min_notional_value=Decimal("5"),
        min_leverage=Decimal("1"),
        max_leverage=Decimal("10"),
        leverage_step=Decimal("1"),
        tick_size=Decimal("0.0001"),
        turnover_24h=Decimal("1000000"),
        spread_bps=Decimal("2"),
        bid_depth_usdt=Decimal("10000"),
        ask_depth_usdt=Decimal("10000"),
        market_timestamp=datetime.now(timezone.utc),
    )


def test_quantity_step_normalization_and_minimum_notional() -> None:
    quantity = normalize_order_quantity(
        Decimal("100"), Decimal("0.51"), _instrument(), Decimal("100")
    )
    assert quantity == Decimal("196")
    accepted = quantity * Decimal("0.51")
    assert accepted == Decimal("99.96")
    assert accepted < Decimal("100")


def test_normalized_notional_above_100_is_valid() -> None:
    quantity = normalize_sized_order_quantity(
        Decimal("100"),
        Decimal("100"),
        Decimal("0.51"),
        _instrument(),
        Decimal("150"),
    )
    assert quantity == Decimal("197")
    assert quantity * Decimal("0.51") == Decimal("100.47")


def test_minimum_round_up_never_exceeds_hard_cap() -> None:
    with pytest.raises(ValueError, match="hard sizing cap"):
        normalize_sized_order_quantity(
            Decimal("100"),
            Decimal("100"),
            Decimal("0.51"),
            _instrument(),
            Decimal("100"),
        )


def test_safe_or_normalized_notional_below_100_is_rejected() -> None:
    decision = _size(_candidate(stop=Decimal("6")))
    assert decision.rejection_code == "SAFE_NOTIONAL_BELOW_MINIMUM"
    assert decision.requested_notional_usdt < Decimal("100")


def test_fees_and_slippage_reduce_edge_cap_and_size() -> None:
    cheap = _size(_candidate(gross_edge=Decimal("35"), fees=Decimal("4"), slippage=Decimal("2")))
    costly = _size(_candidate(gross_edge=Decimal("35"), fees=Decimal("18"), slippage=Decimal("8")))
    assert cheap.requested_notional_usdt > costly.requested_notional_usdt


def test_duplicate_candidate_does_not_reserve_capital_twice() -> None:
    portfolio = PortfolioRiskService(_settings())
    candidate_id = uuid4()
    first = portfolio.reserve(
        run_id="r",
        candidate_id=candidate_id,
        symbol=Symbol.XRPUSDT,
        strategy_name=StrategyName.VOLUME_BREAKOUT,
        notional=Decimal("150"),
        risk_usdt=Decimal("1"),
    )
    second = portfolio.reserve(
        run_id="r",
        candidate_id=candidate_id,
        symbol=Symbol.XRPUSDT,
        strategy_name=StrategyName.VOLUME_BREAKOUT,
        notional=Decimal("150"),
        risk_usdt=Decimal("1"),
    )
    assert first is second
    assert len(portfolio.reservations) == 1


class _RestartRepository:
    def __init__(self) -> None:
        self.state = None
        self.executions: list[DemoExecutionRecord] = []

    def load_v2_portfolio_state(self):
        return self.state

    def save_v2_portfolio_state(self, state):
        self.state = state
        self.state["reservations"] = [
            item.model_dump(mode="json") for item in self.reservations
        ] if hasattr(self, "reservations") else self.state.get("reservations", [])
        return True

    def load_demo_executions(self):
        return self.executions

    def update_v2_portfolio_reservation(self, reservation):
        return True


def test_restart_preserves_open_reservation_and_completed_trade_releases_once() -> None:
    repo = _RestartRepository()
    service = PortfolioRiskService(_settings(), repo)
    reservation = service.reserve(
        run_id="r",
        candidate_id=uuid4(),
        symbol=Symbol.XRPUSDT,
        strategy_name=StrategyName.VOLUME_BREAKOUT,
        notional=Decimal("200"),
        risk_usdt=Decimal("1"),
    )
    assert reservation
    execution_id = uuid4()
    service.mark_open(reservation.id, execution_id)
    repo.reservations = service.reservations
    service._persist_state()
    repo.executions = [
        DemoExecutionRecord(
            id=execution_id,
            candidate_id=reservation.candidate_id,
            run_id="r",
            order_link_id="entry",
            state=DemoExecutionState.DEMO_POSITION_OPEN,
            symbol=Symbol.XRPUSDT,
            side=Side.BUY,
            requested_quantity=Decimal("200"),
        )
    ]
    restored = PortfolioRiskService(_settings(), repo)
    assert sum(
        item.notional_usdt for item in restored.reservations
        if item.state in restored.ACTIVE
    ) == Decimal("200")
    restored.release(reservation.id)
    restored.release(reservation.id)
    assert sum(
        item.notional_usdt for item in restored.reservations
        if item.state in restored.ACTIVE
    ) == Decimal("0")


def test_restart_releases_reservation_for_terminal_execution_once() -> None:
    repo = _RestartRepository()
    service = PortfolioRiskService(_settings(), repo)
    reservation = service.reserve(
        run_id="r",
        candidate_id=uuid4(),
        symbol=Symbol.SOLUSDT,
        strategy_name=StrategyName.VOLUME_BREAKOUT,
        notional=Decimal("200"),
        risk_usdt=Decimal("1"),
    )
    assert reservation
    execution_id = uuid4()
    service.mark_open(reservation.id, execution_id)
    repo.reservations = service.reservations
    service._persist_state()
    repo.executions = [
        DemoExecutionRecord(
            id=execution_id,
            candidate_id=reservation.candidate_id,
            run_id="r",
            order_link_id="entry",
            state=DemoExecutionState.DEMO_CLOSED,
            symbol=Symbol.SOLUSDT,
            side=Side.BUY,
            requested_quantity=Decimal("2.6"),
            closed_at=datetime.now(timezone.utc),
        )
    ]
    restored = PortfolioRiskService(_settings(), repo)
    assert restored.reservations[0].state == ReservationState.RELEASED
    restored_again = PortfolioRiskService(_settings(), repo)
    assert restored_again.reservations[0].state == ReservationState.RELEASED


def test_risk_capital_change_rebases_equity_without_false_drawdown_kill() -> None:
    repo = _RestartRepository()
    repo.state = {
        "reservations": [],
        "kill_switch_active": False,
        "kill_switch_reasons": [],
        "cumulative_realized_pnl": "-0.20",
        "unrealized_pnl": "0",
        "equity": "1999.80",
        "peak_equity": "2000.30",
        "realized_events": {
            "closed-execution": {
                "closed_at": datetime.now(timezone.utc).isoformat(),
                "pnl": "-0.20",
            }
        },
    }
    service = PortfolioRiskService(_settings(risk_capital_usdt=Decimal("1000")), repo)
    assert service.kill_switch_active is False
    assert service.equity == Decimal("999.80")
    assert service.peak_equity == Decimal("1000.30")
    assert service.current_drawdown_pct < Decimal("0.1")
    assert repo.state["risk_capital_usdt"] == "1000"


def test_sizing_is_exported_to_signal_and_trade_rows() -> None:
    candidate = _candidate()
    candidate.sizing = _size(candidate)
    signal = _signal_row(candidate.model_dump(mode="json"))
    assert signal["sizing_requested_notional_usdt"] == "300"
    record = DemoExecutionRecord(
        candidate_id=candidate.id,
        run_id="r",
        order_link_id="entry",
        state=DemoExecutionState.DEMO_CLOSED,
        symbol=Symbol.BTCUSDT,
        side=Side.BUY,
        requested_quantity=Decimal("0.004"),
        accepted_quantity=Decimal("0.004"),
        average_fill_price=Decimal("65000"),
        sizing_details=candidate.sizing.model_dump(mode="json"),
    )
    trade = _trade_row(record.model_dump(mode="json"))
    assert trade["sizing_confidence_tier"] == "above_0.80"
    assert trade["actual_accepted_notional_usdt"] == "260.000"
