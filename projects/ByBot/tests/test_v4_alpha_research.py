from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.config import Settings
from app.db.persistence import PersistenceRepository
from app.models import Symbol
from app.v2.models import MarketFeatureSnapshot, SourceHealth
from app.v4.models import V4Decision, V4ForwardLabel, V4Opportunity
from app.v4.research import (
    build_forward_label,
    build_opportunity,
    chronological_splits,
    cost_components,
    deterministic_opportunity_id,
    feature_availability,
)
from app.v4.shadow import V4ShadowCollector


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def snapshot(
    *, at: datetime = NOW, price: str = "100", momentum: str = "8",
    fresh: bool = True, volume: str = "2", order_flow: str = "0.1",
) -> MarketFeatureSnapshot:
    windows = ("10s", "30s", "1m", "3m", "5m", "15m", "1h")
    return MarketFeatureSnapshot(
        symbol=Symbol.BTCUSDT,
        timestamp=at,
        fresh=fresh,
        stale_reasons=[] if fresh else ["ticker or orderbook is stale"],
        last_price=Decimal(price),
        bid_price=Decimal(price) - Decimal("0.01"),
        ask_price=Decimal(price) + Decimal("0.01"),
        spread_bps=Decimal("2"),
        bid_depth_usdt=Decimal("10000"),
        ask_depth_usdt=Decimal("10000"),
        bid_depth_10bps_usdt=Decimal("5000"),
        ask_depth_10bps_usdt=Decimal("5000"),
        price_momentum={key: Decimal(momentum) for key in windows},
        breakout_distance_bps={key: Decimal("1") for key in windows},
        volume_acceleration={key: Decimal(volume) for key in windows},
        trade_imbalance={key: Decimal("0.2") for key in windows},
        order_flow_imbalance={key: Decimal(order_flow) for key in windows},
        orderbook_imbalance=Decimal("0.1"),
        microprice=Decimal(price),
        realized_volatility={
            **{key: Decimal("1") for key in windows},
            "1m": Decimal("1.3"), "15m": Decimal("1"),
        },
        observation_count={key: 20 for key in windows},
        window_coverage_seconds={key: Decimal("60") for key in windows},
        atr_bps=Decimal("25"),
        distance_from_high_bps=Decimal("-2"),
        distance_from_low_bps=Decimal("18"),
        funding_rate=Decimal("0.0001"),
        funding_deviation_bps=Decimal("1"),
        open_interest=Decimal("100000"),
        open_interest_change_pct=Decimal("0.2"),
        volume_24h=Decimal("100000000"),
        market_regime="TRENDING_UP",
        source_health={
            key: SourceHealth.OK for key in (
                "ticker", "trades", "orderbook", "funding",
                "open_interest", "liquidations",
            )
        },
        source_timestamps={
            key: at - timedelta(seconds=1) for key in (
                "ticker", "trades", "orderbook", "funding",
                "open_interest", "liquidations",
            )
        },
        source_age_seconds={
            key: 1.0 for key in (
                "ticker", "trades", "orderbook", "funding",
                "open_interest", "liquidations",
            )
        },
        liquidation_feed_initialized=True,
        liquidation_feed_available=True,
    )


def test_v4_flags_are_disabled_and_shadow_only_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.v4_alpha_enabled is False
    assert settings.v4_alpha_shadow_only is True
    with pytest.raises(ValueError, match="SHADOW_ONLY"):
        Settings(
            _env_file=None, v4_alpha_enabled=True, v4_alpha_shadow_only=False
        )


def test_opportunity_id_is_deterministic_and_changes_by_side() -> None:
    common = {
        "run_id": "run", "cycle_id": "cycle", "symbol": "BTCUSDT",
        "snapshot_time": NOW,
    }
    first = deterministic_opportunity_id(side="BUY", **common)
    assert first == deterministic_opportunity_id(side="BUY", **common)
    assert first != deterministic_opportunity_id(side="SELL", **common)


def test_selected_shadow_opportunity_has_no_exchange_timestamps() -> None:
    row = build_opportunity(snapshot(), run_id="run", cycle_id="cycle")
    assert row.decision == V4Decision.SHADOW_TRADE
    assert row.shadow_only is True
    assert row.executed is False
    assert row.order_submit_time is None
    assert row.order_ack_time is None
    assert row.first_fill_time is None
    assert row.candidate_layers["G_FULL_COST_AWARE"] is True


def test_rejected_and_stale_opportunities_are_still_tape_rows() -> None:
    row = build_opportunity(
        snapshot(fresh=False, momentum="0", volume="0"),
        run_id="run", cycle_id="cycle",
    )
    reasons = {item.value for item in row.rejection_reasons}
    assert row.decision == V4Decision.NO_TRADE
    assert "DATA_STALE" in reasons
    assert "NO_VOLATILITY_EXPANSION" in reasons
    assert row.opportunity_id is not None


def test_canonical_tape_can_represent_observed_execution_without_enabling_it() -> None:
    shadow = build_opportunity(snapshot(), run_id="historical", cycle_id="cycle")
    payload = shadow.model_dump(mode="python")
    payload.update({
        "shadow_only": False,
        "executed": True,
        "decision": V4Decision.EXECUTED_OBSERVED,
        "order_submit_time": NOW + timedelta(milliseconds=10),
        "order_ack_time": NOW + timedelta(milliseconds=20),
        "first_fill_time": NOW + timedelta(milliseconds=30),
    })
    observed = V4Opportunity.model_validate(payload)
    assert observed.executed is True
    assert observed.first_fill_time is not None


def test_unavailable_historical_features_remain_unknown() -> None:
    row = build_opportunity(snapshot(), run_id="run", cycle_id="cycle")
    assert row.features["return_5s_bps"] is None
    assert row.features["funding_delta"] is None
    assert row.availability["return_5s_bps"].value == "UNKNOWN"
    assert row.availability["momentum_1m_bps"].value == "PRE_ENTRY_AVAILABLE"


def test_feature_snapshot_cannot_use_later_source_timestamp() -> None:
    row = build_opportunity(snapshot(), run_id="run", cycle_id="cycle")
    assert all(
        timing.source_timestamp is None
        or timing.source_timestamp <= row.feature_snapshot_time
        for timing in row.feature_timing.values()
    )


def test_forward_label_buy_return_mfe_mae_and_barrier_order() -> None:
    opportunity = build_opportunity(snapshot(), run_id="run", cycle_id="cycle")
    path = [
        (NOW + timedelta(seconds=10), Decimal("100.20")),
        (NOW + timedelta(seconds=20), Decimal("99.80")),
        (NOW + timedelta(seconds=30), Decimal("100.10")),
    ]
    label = build_forward_label(opportunity, path, generated_at=NOW + timedelta(seconds=900))
    assert Decimal(label.labels["gross_forward_bps_30s"]) == Decimal("10.0")
    assert Decimal(label.labels["mfe_bps_30s"]) == Decimal("20.0")
    assert Decimal(label.labels["mae_bps_30s"]) == Decimal("-20.0")
    assert label.labels["barrier_plus_15_before_minus_10"] == "TARGET"
    assert label.first_fill_time is None


def test_forward_label_sell_is_direction_adjusted() -> None:
    opportunity = build_opportunity(
        snapshot(momentum="-8", order_flow="-0.1"),
        run_id="run", cycle_id="cycle",
    )
    label = build_forward_label(
        opportunity,
        [(NOW + timedelta(seconds=15), Decimal("99.80"))],
        generated_at=NOW + timedelta(seconds=900),
    )
    assert Decimal(label.labels["gross_forward_bps_15s"]) == Decimal("20.0")


def test_missing_path_remains_unknown_and_never_fabricates_fill() -> None:
    opportunity = build_opportunity(snapshot(), run_id="run", cycle_id="cycle")
    label = build_forward_label(opportunity, [], generated_at=NOW + timedelta(seconds=900))
    assert label.labels["coverage_15s"] == "UNKNOWN"
    assert label.labels["gross_forward_bps"] is None
    assert label.observation_count == 0
    assert label.first_fill_time is None


def test_cost_components_are_separate_and_stress_is_deterministic() -> None:
    components = cost_components(
        maker_taker_fees_bps=Decimal("4"), spread_bps=Decimal("2"),
        estimated_slippage_bps=Decimal("3"), funding_bps=Decimal("1"),
        other_execution_costs_bps=Decimal("0.5"),
    )
    assert components["modeled_total_bps"] == Decimal("10.5")
    opportunity = build_opportunity(snapshot(), run_id="run", cycle_id="cycle")
    label = build_forward_label(
        opportunity,
        [(NOW + timedelta(seconds=300), Decimal("100.20"))],
        generated_at=NOW + timedelta(seconds=900), components=components,
    )
    assert Decimal(label.labels["net_forward_bps_cost_11"]) == Decimal("9")
    assert Decimal(label.labels["net_forward_bps_cost_15"]) == Decimal("5")


def test_rejected_opportunity_receives_same_independent_market_path_label() -> None:
    opportunity = build_opportunity(
        snapshot(fresh=False, momentum="0"), run_id="run", cycle_id="cycle"
    )
    label = build_forward_label(
        opportunity,
        [(NOW + timedelta(seconds=60), Decimal("100.10"))],
        generated_at=NOW + timedelta(seconds=900),
    )
    assert opportunity.decision == V4Decision.NO_TRADE
    assert label.labels["coverage_60s"] == "OBSERVED"


def test_chronological_split_freezes_holdout_and_purges_overlapping_labels() -> None:
    opportunities = [
        build_opportunity(
            snapshot(at=NOW + timedelta(minutes=index)),
            run_id="run", cycle_id=f"cycle-{index}",
        )
        for index in range(100)
    ]
    splits = chronological_splits(opportunities)
    assert len(splits["holdout_ids"]) == 20
    assert splits["holdout_used_for_selection"] is False
    assert set(splits["development_ids"]).isdisjoint(splits["holdout_ids"])
    for fold in splits["folds"]:
        assert set(fold["train_ids"]).isdisjoint(fold["validation_ids"])
        assert fold["preprocessing_fit_scope"] == "TRAIN_ONLY"


class ShadowRepository:
    def __init__(self) -> None:
        self.opportunities = []
        self.labels = []
        self.exchange_calls = 0
        self.capacity_calls = 0
        self.risk_calls = 0

    def save_v4_opportunity(self, row: object) -> bool:
        self.opportunities.append(row)
        return True

    def save_v4_forward_label(self, row: object) -> bool:
        self.labels.append(row)
        return True


def test_shadow_collector_only_writes_research_rows() -> None:
    repository = ShadowRepository()
    settings = Settings(
        _env_file=None, v4_alpha_enabled=True, v4_alpha_shadow_only=True,
        v4_opportunity_cadence_seconds=60,
    )
    collector = V4ShadowCollector(settings, repository, run_id="run")
    first = collector.observe(snapshot(), cycle_id="cycle-1")
    collector.observe(
        snapshot(at=NOW + timedelta(seconds=900), price="100.2"),
        cycle_id="cycle-2",
    )
    assert first is not None
    assert len(repository.opportunities) == 2
    assert repository.labels
    assert repository.exchange_calls == 0
    assert repository.capacity_calls == 0
    assert repository.risk_calls == 0
    status = collector.status()
    assert status["exchange_mutations"] == 0
    assert status["capacity_mutations"] == 0
    assert status["production_risk_mutations"] == 0


def test_opportunity_and_label_persistence_are_idempotent() -> None:
    repository = PersistenceRepository("sqlite+pysqlite:///:memory:", create_schema=True)
    opportunity = build_opportunity(snapshot(), run_id="run", cycle_id="cycle")
    label = build_forward_label(
        opportunity,
        [(NOW + timedelta(seconds=300), Decimal("100.20"))],
        generated_at=NOW + timedelta(seconds=900),
    )
    assert repository.save_v4_opportunity(opportunity) is True
    assert repository.save_v4_opportunity(opportunity) is True
    assert repository.save_v4_forward_label(label) is True
    assert repository.save_v4_forward_label(label) is True
    loaded = repository.load_v4_opportunities("run")
    assert [row.opportunity_id for row in loaded] == [opportunity.opportunity_id]
