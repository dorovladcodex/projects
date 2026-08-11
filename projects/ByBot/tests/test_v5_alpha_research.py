from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, select

from app.config import Settings
from app.v5.models import DataAvailability, FundingPayment, MarketLegSnapshot
from app.v5.research import (
    CarryPathPoint,
    PricePoint,
    beta_hedged_pair_return_bps,
    build_carry_label,
    build_carry_opportunity,
    build_non_overlapping_momentum,
    calculate_basis_bps,
    chronological_folds,
    cost_stress,
    execution_scenarios,
    fit_beta_train_only,
    funding_cashflow,
)
from app.v5.shadow import V5CarryShadowCollector


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def leg(
    *, category: str, symbol: str, bid: str, ask: str,
    source_at: datetime = NOW, received_at: datetime | None = None,
) -> MarketLegSnapshot:
    return MarketLegSnapshot(
        symbol=symbol,
        category=category,
        source_timestamp=source_at,
        received_at=received_at or source_at + timedelta(milliseconds=100),
        bid=Decimal(bid),
        ask=Decimal(ask),
        mark_price=Decimal("101") if category == "linear" else None,
        index_price=Decimal("100") if category == "linear" else None,
        bid_depth_usdt=Decimal("50000"),
        ask_depth_usdt=Decimal("50000"),
        slippage_bps_by_notional={
            "100": Decimal("0.5"), "1000": Decimal("0.5"),
        },
    )


def complete_opportunity():
    return build_carry_opportunity(
        symbol="BTCUSDT",
        spot=leg(category="spot", symbol="BTCUSDT", bid="99", ask="100"),
        perp=leg(category="linear", symbol="BTCUSDT", bid="101", ask="102"),
        current_funding_rate=Decimal("0.001"),
        predicted_funding_rate=Decimal("0.0008"),
        next_funding_time=NOW + timedelta(hours=8),
        funding_interval_hours=Decimal("8"),
        historical_funding={
            "previous_period": Decimal("0.0009"),
            "rolling_24h": Decimal("0.0027"),
            "rolling_3d": Decimal("0.007"),
            "rolling_7d": Decimal("0.014"),
        },
        account_fees_bps={
            "spot_maker": Decimal("0.5"),
            "spot_taker": Decimal("1"),
            "perp_maker": Decimal("0.5"),
            "perp_taker": Decimal("1"),
        },
    )


def test_v5_defaults_are_disabled_shadow_only_and_fees_unknown() -> None:
    settings = Settings(_env_file=None)
    assert settings.v5_alpha_enabled is False
    assert settings.v5_alpha_shadow_only is True
    assert settings.v5_spot_maker_fee_bps is None
    assert settings.v5_perp_taker_fee_bps is None
    with pytest.raises(ValueError, match="V5_ALPHA_ENABLED"):
        Settings(_env_file=None, v5_alpha_enabled=True, v5_alpha_shadow_only=False)


def test_basis_and_spot_perp_alignment_are_exact() -> None:
    assert calculate_basis_bps(Decimal("100"), Decimal("101")) == Decimal("100")
    opportunity = complete_opportunity()
    assert opportunity.basis_bps == calculate_basis_bps(
        Decimal("99.5"), Decimal("101.5")
    )
    assert opportunity.alignment_ms == Decimal("0.0")
    assert opportunity.availability["aligned_prices"] == DataAvailability.AVAILABLE


def test_market_snapshot_rejects_future_source_and_crossed_book() -> None:
    with pytest.raises(ValueError, match="after receipt"):
        leg(
            category="spot", symbol="BTCUSDT", bid="99", ask="100",
            source_at=NOW + timedelta(seconds=1), received_at=NOW,
        )
    with pytest.raises(ValueError, match="bid cannot exceed"):
        leg(category="spot", symbol="BTCUSDT", bid="101", ask="100")


def test_partial_inputs_remain_unknown_and_no_fee_is_invented() -> None:
    opportunity = build_carry_opportunity(
        symbol="BTCUSDT",
        spot=leg(category="spot", symbol="BTCUSDT", bid="99", ask="100"),
        perp=leg(category="linear", symbol="BTCUSDT", bid="101", ask="102"),
    )
    assert "ACCOUNT_FEE_CONFIGURATION_MISSING" in opportunity.blockers
    assert "PREDICTED_FUNDING_MISSING" in opportunity.blockers
    assert opportunity.account_fees_bps["spot_taker"] is None
    label = build_carry_label(
        opportunity,
        [],
        [],
        horizon="1_interval",
        horizon_end=NOW + timedelta(hours=8),
        notional_usdt=Decimal("1000"),
    )
    assert label.coverage == DataAvailability.UNKNOWN
    assert label.net_carry_pnl is None
    assert label.first_fill_time is None


def test_funding_sign_and_variable_intervals() -> None:
    assert funding_cashflow(
        notional=Decimal("1000"), rate=Decimal("0.001"), perp_side="SHORT"
    ) == Decimal("1")
    assert funding_cashflow(
        notional=Decimal("1000"), rate=Decimal("0.001"), perp_side="LONG"
    ) == Decimal("-1")
    opportunity = complete_opportunity()
    payments = [
        FundingPayment(
            timestamp=opportunity.timestamp + timedelta(hours=4),
            rate=Decimal("0.001"), interval_hours=Decimal("4"), source="fixture",
        ),
        FundingPayment(
            timestamp=opportunity.timestamp + timedelta(hours=12),
            rate=Decimal("-0.0005"), interval_hours=Decimal("8"), source="fixture",
        ),
    ]
    end_at = opportunity.timestamp + timedelta(hours=12)
    label = build_carry_label(
        opportunity,
        [CarryPathPoint(
            timestamp=end_at,
            spot_bid=Decimal("102"), spot_ask=Decimal("103"),
            perp_bid=Decimal("99"), perp_ask=Decimal("100"),
        )],
        payments,
        horizon="2_variable_intervals",
        horizon_end=end_at,
        notional_usdt=Decimal("1000"),
    )
    assert label.coverage == DataAvailability.AVAILABLE
    assert label.funding_income == Decimal("0.5")
    assert label.funding_received == Decimal("1")
    assert label.funding_paid == Decimal("0.5")
    assert label.funding_sign_flip is True
    assert label.details["funding_payment_count"] == 2


def test_delta_neutral_carry_accounts_each_leg_and_all_costs() -> None:
    opportunity = complete_opportunity()
    end_at = opportunity.timestamp + timedelta(hours=8)
    label = build_carry_label(
        opportunity,
        [CarryPathPoint(
            timestamp=end_at,
            spot_bid=Decimal("102"), spot_ask=Decimal("103"),
            perp_bid=Decimal("99"), perp_ask=Decimal("100"),
        )],
        [FundingPayment(
            timestamp=end_at,
            rate=Decimal("0.001"), interval_hours=Decimal("8"), source="fixture",
        )],
        horizon="1_interval",
        horizon_end=end_at,
        notional_usdt=Decimal("1000"),
    )
    assert label.spot_leg_pnl == Decimal("20")
    assert label.perp_leg_pnl == Decimal("1000") / Decimal("101")
    assert label.funding_income == Decimal("1")
    assert label.entry_cost == Decimal("0.2")
    assert label.exit_cost is not None and label.exit_cost > 0
    assert label.estimated_slippage == Decimal("0.2")
    assert label.net_carry_pnl == (
        label.hedged_gross_pnl - label.entry_cost - label.exit_cost - label.estimated_slippage
    )
    assert label.max_hedge_imbalance_bps is not None


def test_execution_scenarios_never_assume_maker_fill() -> None:
    scenarios = execution_scenarios(complete_opportunity(), notional_usdt=Decimal("100"))
    assert scenarios["TAKER_TAKER"]["status"] == "MODELED"
    assert scenarios["TAKER_TAKER"]["round_trip_cost_bps"] == "6.0"
    for name in ("MAKER_TAKER", "TAKER_MAKER", "MAKER_MAKER"):
        assert scenarios[name]["status"] == "INSUFFICIENT_EXECUTION_DATA"
        assert scenarios[name]["maker_fill_assumed"] is False


def test_long_horizon_observations_are_controlled_and_non_overlapping() -> None:
    points = [
        PricePoint(NOW + timedelta(minutes=30 * index), Decimal(100 + index))
        for index in range(12)
    ]
    rows = build_non_overlapping_momentum(
        "BTCUSDT", points, horizon_seconds=1800, endpoint_tolerance_seconds=1,
    )
    assert rows
    assert all(
        (right.timestamp - left.timestamp).total_seconds() >= 1800
        for left, right in zip(rows, rows[1:])
    )
    first = rows[0]
    assert first.past_return_bps > 0
    assert first.future_return_bps > 0
    assert first.net_strategy_bps == first.gross_strategy_bps - Decimal("11")


def test_chronological_folds_freeze_holdout_and_purge_labels() -> None:
    points = [
        PricePoint(NOW + timedelta(hours=index), Decimal(100 + index))
        for index in range(130)
    ]
    rows = build_non_overlapping_momentum(
        "BTCUSDT", points, horizon_seconds=3600, endpoint_tolerance_seconds=1,
    )
    split = chronological_folds(rows, folds=4, purge_seconds=3600)
    assert split["holdout_frozen"] is True
    assert split["holdout_used_for_selection"] is False
    assert len(split["folds"]) == 4
    for fold in split["folds"]:
        assert fold["fit_scope"] == "TRAIN_ONLY"
        assert not fold["train"] or (
            fold["train"][-1].timestamp
            < fold["validation"][0].timestamp - timedelta(hours=1)
        )


def test_beta_is_fit_from_supplied_training_rows_only() -> None:
    beta = fit_beta_train_only(
        [Decimal("2"), Decimal("4"), Decimal("6"), Decimal("8")],
        [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")],
    )
    assert beta == Decimal("2.0")
    gross, ratio = beta_hedged_pair_return_bps(
        long_return_bps=Decimal("30"), short_return_bps=Decimal("10"),
        long_beta=Decimal("1.5"), short_beta=Decimal("0.75"),
    )
    assert ratio == Decimal("2")
    assert gross == Decimal("10")


def test_cost_stress_increases_cost_without_changing_gross() -> None:
    result = cost_stress(
        [Decimal("20"), Decimal("10")], base_cost_bps=Decimal("10")
    )
    assert result["1x"]["gross_expectancy_bps"] == "15"
    assert result["1x"]["net_expectancy_bps"] == "5"
    assert result["2x"]["net_expectancy_bps"] == "-5"


class Sink:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def write(self, payload: dict[str, object]) -> None:
        self.rows.append(payload)


def test_shadow_collector_has_no_execution_capacity_risk_or_db_mutation() -> None:
    settings = Settings(
        _env_file=None,
        v5_alpha_enabled=True,
        v5_alpha_shadow_only=True,
        v5_spot_maker_fee_bps=Decimal("1"),
        v5_spot_taker_fee_bps=Decimal("1"),
        v5_perp_maker_fee_bps=Decimal("1"),
        v5_perp_taker_fee_bps=Decimal("1"),
    )
    sink = Sink()
    collector = V5CarryShadowCollector(settings, sink, notionals=(Decimal("100"),))
    opportunity = collector.observe(
        symbol="BTCUSDT",
        spot=leg(category="spot", symbol="BTCUSDT", bid="99", ask="100"),
        perp=leg(category="linear", symbol="BTCUSDT", bid="101", ask="102"),
        current_funding_rate=Decimal("0.001"),
        predicted_funding_rate=None,
        next_funding_time=NOW + timedelta(hours=8),
        funding_interval_hours=Decimal("8"),
    )
    assert opportunity is not None
    assert opportunity.executed is False
    assert any(row["record_type"] == "HYPOTHETICAL_MAKER_QUOTE" for row in sink.rows)
    status = collector.status()
    assert status["exchange_mutations"] == 0
    assert status["capacity_mutations"] == 0
    assert status["production_risk_mutations"] == 0
    assert status["database_mutations"] == 0
    quote = next(row for row in sink.rows if row["record_type"] == "HYPOTHETICAL_MAKER_QUOTE")
    outcome = collector.record_post_quote_market(
        quote_id=str(quote["quote_id"]),
        observed_at=opportunity.timestamp + timedelta(seconds=60),
        spot_mid=Decimal("98"),
        perp_mid=Decimal("103"),
    )
    assert outcome is not None
    assert Decimal(str(outcome["combined_adverse_selection_bps"])) > 0
    assert outcome["fill_observed"] is False


def test_0015_upgrade_and_downgrade_preserve_historical_table(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite"
    engine = create_engine(f"sqlite+pysqlite:///{database}")
    metadata = MetaData()
    historical = Table(
        "v2_market_feature_snapshots", metadata,
        Column("id", Integer, primary_key=True),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(historical.insert().values(id=7))
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        path = Path("alembic/versions/20260811_0015_v4_alpha_research.py")
        spec = importlib.util.spec_from_file_location("v5_migration_0015", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.op = operations
        module.upgrade()
        tables = set(inspect(connection).get_table_names())
        assert {"v4_opportunities", "v4_forward_labels"} <= tables
        assert connection.scalar(select(historical.c.id)) == 7
        module.downgrade()
        tables = set(inspect(connection).get_table_names())
        assert "v4_opportunities" not in tables
        assert "v4_forward_labels" not in tables
        assert "v2_market_feature_snapshots" in tables
        assert connection.scalar(select(historical.c.id)) == 7
