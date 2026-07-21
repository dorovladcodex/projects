from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from app.config import Settings
from app.models import DemoExecutionRecord, DemoExecutionState, Side, Symbol
from app.v2.execution import V2ExecutionCoordinator, calculate_risk_target_notional
from app.v2.market import RollingFeatureEngine
from app.v2.models import MarketFeatureSnapshot, SourceHealth, StrategySide
from app.v2.portfolio import PortfolioRiskService
from app.v2.research import (
    CalibrationObservation,
    EmpiricalEdgeCalibrator,
    bootstrap_mean_confidence_interval,
    purged_walk_forward_splits,
    triple_barrier_label,
)
from app.v2.scoring import CommonScoringPipeline
from app.v2.strategies import (
    MEME_SYMBOLS,
    NewsStrategyContext,
    build_v2_strategies,
)


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "v2_enabled": True,
        "allowed_symbols": tuple(symbol.value for symbol in Symbol),
        "v2_min_orderbook_depth_usdt": Decimal("100"),
        "v2_min_liquidation_notional_usdt": Decimal("100"),
        "v2_min_feature_observations": 2,
        "v2_min_feature_coverage_pct": Decimal("1"),
    }
    values.update(updates)
    return Settings(**values)


def _feature(
    *, direction: int = 1, symbol: Symbol = Symbol.BTCUSDT,
    regime: str | None = None,
) -> MarketFeatureSnapshot:
    sign = Decimal(direction)
    now = datetime.now(timezone.utc)
    windows = ("10s", "30s", "1m", "3m", "5m", "15m", "1h")
    return MarketFeatureSnapshot(
        symbol=symbol,
        timestamp=now,
        fresh=True,
        last_price=Decimal("100"),
        bid_price=Decimal("99.99"),
        ask_price=Decimal("100.01"),
        spread_bps=Decimal("2"),
        bid_depth_usdt=Decimal("100000"),
        ask_depth_usdt=Decimal("100000"),
        bid_depth_10bps_usdt=Decimal("10000"),
        ask_depth_10bps_usdt=Decimal("10000"),
        price_momentum={key: sign * Decimal("50") for key in windows},
        breakout_distance_bps={key: sign * Decimal("40") for key in windows},
        volume_acceleration={key: Decimal("3") for key in windows},
        trade_imbalance={key: sign * Decimal("0.8") for key in windows},
        order_flow_imbalance={key: sign * Decimal("0.7") for key in windows},
        orderbook_imbalance=sign * Decimal("0.6"),
        realized_volatility={key: Decimal("10") for key in windows},
        atr_bps=Decimal("25"),
        distance_from_high_bps=Decimal("-20"),
        distance_from_low_bps=Decimal("100"),
        relative_strength_vs_btc_bps=sign * Decimal("40"),
        funding_rate=-sign * Decimal("0.0005"),
        funding_deviation_bps=-sign * Decimal("5"),
        open_interest=Decimal("1000000"),
        open_interest_change_pct=Decimal("3"),
        liquidation_short_usdt=Decimal("5000") if direction > 0 else Decimal("0"),
        liquidation_long_usdt=Decimal("5000") if direction < 0 else Decimal("0"),
        liquidation_imbalance=sign,
        liquidation_event_count_5m=2,
        liquidation_notional_5m=Decimal("5000"),
        liquidation_data_age_seconds=1,
        volume_24h=Decimal("1000000"),
        market_regime=regime or ("TRENDING_UP" if direction > 0 else "TRENDING_DOWN"),
        source_health={
            "ticker": SourceHealth.OK,
            "trades": SourceHealth.OK,
            "orderbook": SourceHealth.OK,
        },
        source_timestamps={"funding": now, "open_interest": now},
        source_age_seconds={"funding": 1, "open_interest": 1},
    )


def test_liquidation_position_side_mapping_matches_bybit_semantics() -> None:
    engine = RollingFeatureEngine(_settings())
    now = datetime.now(timezone.utc)
    engine.ingest_ticker(Symbol.BTCUSDT, {"lastPrice": "100", "volume24h": "1000"}, now)
    engine.ingest_orderbook(Symbol.BTCUSDT, [["99.9", "10"]], [["100.1", "10"]], now)
    engine.ingest_trade(Symbol.BTCUSDT, Decimal("100"), Decimal("1"), "Buy", now)
    engine.ingest_liquidation(Symbol.BTCUSDT, "Buy", Decimal("100"), Decimal("2"), now)
    snapshot = engine.snapshot(Symbol.BTCUSDT, now=now)
    assert snapshot is not None
    assert snapshot.liquidation_long_usdt == Decimal("200")
    assert snapshot.liquidation_short_usdt == 0
    assert snapshot.liquidation_imbalance < 0


def test_common_confirmation_and_relative_strength_are_side_aware() -> None:
    pipeline = CommonScoringPipeline(_settings())
    bearish = _feature(direction=-1)
    long_score = pipeline.score(Decimal("0.7"), bearish, side=StrategySide.LONG)
    short_score = pipeline.score(Decimal("0.7"), bearish, side=StrategySide.SHORT)
    assert short_score.market_confirmation_score > long_score.market_confirmation_score
    assert short_score.relative_strength_score > long_score.relative_strength_score


def test_missing_oi_and_funding_are_hard_setup_rejections() -> None:
    feature = _feature().model_copy(deep=True)
    feature.open_interest_change_pct = None
    feature.funding_deviation_bps = None
    strategy = build_v2_strategies(_settings())[2]
    candidate = strategy.evaluate(feature)
    result = CommonScoringPipeline(_settings()).admit(
        candidate, symbol_valid=True, portfolio_reasons=[]
    )
    assert not result.admitted
    assert "open interest" in (result.rejection_reason or "")
    assert "funding" in (result.rejection_reason or "")


def test_liquidation_requires_a_recent_symbol_event() -> None:
    feature = _feature().model_copy(deep=True)
    feature.liquidation_event_count_5m = 0
    feature.liquidation_notional_5m = Decimal("0")
    feature.liquidation_data_age_seconds = None
    candidate = build_v2_strategies(_settings())[3].evaluate(feature)
    assert not candidate.setup_valid
    assert "no recent symbol-specific liquidation event" in candidate.setup_rejection_reasons


def test_neutral_news_never_defines_a_short_trade() -> None:
    candidate = build_v2_strategies(_settings())[0].evaluate(
        _feature(),
        news=NewsStrategyContext("NEUTRAL", Decimal("0.99"), Decimal("1")),
    )
    assert not candidate.setup_valid
    assert candidate.estimated_edge_bps == 0


def test_edge_is_bounded_by_take_profit_and_proxy_is_retained() -> None:
    feature = _feature()
    feature.volume_acceleration = {
        key: Decimal("1000000") for key in feature.volume_acceleration
    }
    candidate = build_v2_strategies(_settings())[1].evaluate(feature)
    assert candidate.edge_proxy_bps > 0
    assert candidate.estimated_edge_bps <= candidate.take_profit_pct * Decimal("100")
    assert candidate.estimated_edge_bps <= _settings().v2_max_empirical_edge_bps


def test_doge_is_consistently_classified_as_meme() -> None:
    settings = _settings()
    assert Symbol.DOGEUSDT in MEME_SYMBOLS
    assert settings.v2_leverage_for_symbol("DOGEUSDT") == settings.meme_leverage
    assert settings.v2_target_notional_for_symbol("DOGEUSDT") == settings.meme_position_notional_usdt


def test_risk_target_notional_uses_stop_and_liquidity_caps() -> None:
    settings = _settings(
        risk_capital_usdt=Decimal("2000"),
        v2_per_trade_risk_pct=Decimal("0.25"),
        max_total_notional_usdt=Decimal("5000"),
    )
    wide_stop = calculate_risk_target_notional(
        settings,
        stop_loss_pct=Decimal("5"),
        category_target_notional=Decimal("5000"),
        executable_depth_usdt=Decimal("100000"),
        active_notional_usdt=Decimal("0"),
    )
    shallow_book = calculate_risk_target_notional(
        settings,
        stop_loss_pct=Decimal("0.1"),
        category_target_notional=Decimal("5000"),
        executable_depth_usdt=Decimal("1000"),
        active_notional_usdt=Decimal("0"),
    )
    assert wide_stop == Decimal("100")
    assert shallow_book == Decimal("50")


def test_final_market_guard_rejects_adverse_price_move() -> None:
    original = _feature()
    moved = original.model_copy(update={
        "timestamp": datetime.now(timezone.utc),
        "ask_price": Decimal("101"),
        "last_price": Decimal("101"),
    })
    coordinator = object.__new__(V2ExecutionCoordinator)
    coordinator.settings = _settings(v2_max_price_deviation_bps=Decimal("10"))
    coordinator.market_snapshot_provider = lambda _symbol: moved
    candidate = build_v2_strategies(coordinator.settings)[1].evaluate(original)
    try:
        coordinator._pre_submit_market_guard(candidate, original.ask_price)
    except Exception as exc:
        assert "price moved" in str(exc)
    else:
        raise AssertionError("adverse move must fail closed")


def test_portfolio_terminal_pnl_is_credited_once_and_restored() -> None:
    class Repo:
        state: dict[str, object] | None = None
        def load_v2_portfolio_state(self): return self.state
        def save_v2_portfolio_state(self, payload): self.state = payload; return True
        def load_demo_executions(self): return []

    repo = Repo()
    service = PortfolioRiskService(_settings(), repo)
    record = DemoExecutionRecord(
        candidate_id=uuid4(), run_id="r", order_link_id="x",
        state=DemoExecutionState.DEMO_CLOSED, symbol=Symbol.BTCUSDT,
        side=Side.BUY, requested_quantity=Decimal("1"),
        realized_exchange_pnl=Decimal("5"), exchange_fees=Decimal("1"),
        closed_at=datetime.now(timezone.utc),
    )
    assert service.apply_execution_result(record)
    assert not service.apply_execution_result(record)
    assert service.equity == service.settings.risk_capital_usdt + Decimal("5")
    restored = PortfolioRiskService(_settings(), repo)
    assert restored.equity == service.equity
    assert restored.cumulative_realized_pnl == Decimal("5")


def test_empirical_calibration_walk_forward_and_bootstrap() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        CalibrationObservation(
            strategy="VolumeBreakoutStrategy", symbol="BTCUSDT",
            regime="TRENDING_UP", net_return_bps=Decimal("10") if i % 3 else Decimal("-4"),
            opened_at=start + timedelta(hours=i * 4),
            closed_at=start + timedelta(hours=i * 4 + 1),
        )
        for i in range(30)
    ]
    candidate = build_v2_strategies(_settings())[1].evaluate(_feature())
    calibrator = EmpiricalEdgeCalibrator(minimum_samples=20)
    calibrator.fit(rows)
    estimate = calibrator.estimate(candidate)
    assert estimate.ready and estimate.sample_count == 30
    assert estimate.win_probability_lower_bound is not None
    assert purged_walk_forward_splits(rows, folds=5)
    lower, upper = bootstrap_mean_confidence_interval(
        [row.net_return_bps for row in rows], samples=200
    )
    assert lower is not None and upper is not None and lower <= upper


def test_triple_barrier_is_chronological_and_net_of_costs() -> None:
    now = datetime.now(timezone.utc)
    result = triple_barrier_label(
        entry_price=Decimal("100"),
        side=StrategySide.LONG,
        future_prices=[
            (now + timedelta(seconds=2), Decimal("99")),
            (now + timedelta(seconds=1), Decimal("102")),
        ],
        stop_loss_pct=Decimal("1"),
        take_profit_pct=Decimal("1"),
        expires_at=now + timedelta(minutes=1),
        round_trip_cost_bps=Decimal("10"),
    )
    assert result == Decimal("190")
