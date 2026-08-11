from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.v2.alpha_lab import (
    chronological_splits,
    compute_path_labels,
    directional_return_bps,
    economic_metrics,
    feature_manifest,
    fit_isotonic,
    spearman,
)


def test_directional_returns_respect_side() -> None:
    assert directional_return_bps(
        Decimal("100"), Decimal("101"), "BUY"
    ) == Decimal("100")
    assert directional_return_bps(
        Decimal("100"), Decimal("99"), "SELL"
    ) == Decimal("100")


def test_path_labels_do_not_invent_missing_early_horizons() -> None:
    fill = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = compute_path_labels(
        fill_at=fill,
        exit_at=fill + timedelta(seconds=100),
        entry_price=Decimal("100"),
        exit_price=Decimal("100.1"),
        side="BUY",
        observations=[
            (fill + timedelta(seconds=61), Decimal("100.2")),
            (fill + timedelta(seconds=90), Decimal("99.8")),
        ],
        durable_mfe_ratio=Decimal("0.002"),
        durable_mae_ratio=Decimal("-0.002"),
    )
    assert result["post__coverage_5s"] == "UNKNOWN"
    assert result["post__coverage_30s"] == "UNKNOWN"
    assert result["post__coverage_120s"] == "OBSERVED"
    assert result["post__mfe_bps_until_exit"] == "20.000"
    assert result["post__mae_bps_until_exit"] == "-20.000"


def test_threshold_order_uses_only_stored_observations() -> None:
    fill = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = compute_path_labels(
        fill_at=fill,
        exit_at=fill + timedelta(seconds=60),
        entry_price=Decimal("100"),
        exit_price=Decimal("100"),
        side="BUY",
        observations=[
            (fill + timedelta(seconds=10), Decimal("100.2")),
            (fill + timedelta(seconds=20), Decimal("99.8")),
        ],
        durable_mfe_ratio=None,
        durable_mae_ratio=None,
    )
    assert result["post__plus_15_before_minus_15"] is True
    assert result["post__first_time_to_plus_15_bps_seconds"] == 10
    assert result["post__first_time_to_minus_15_bps_seconds"] == 20


def test_cost_stress_is_after_cost_and_chronological() -> None:
    rows = [
        {
            "fill_timestamp": "2026-01-01T00:00:00+00:00",
            "accepted_notional": "100",
            "gross_bps": "20",
            "net_pnl": "0.1",
        },
        {
            "fill_timestamp": "2026-01-01T00:01:00+00:00",
            "accepted_notional": "100",
            "gross_bps": "0",
            "net_pnl": "-0.1",
        },
    ]
    result = economic_metrics(rows, cost_bps=Decimal("10"))
    assert result["net_pnl"] == "0.0"
    assert result["gross_bps"] == "10.000"
    assert result["trade_count"] == 2


def test_spearman_detects_monotonic_and_inverted_information() -> None:
    positive = [(Decimal(index), Decimal(index * 2)) for index in range(1, 6)]
    negative = [(Decimal(index), Decimal(-index)) for index in range(1, 6)]
    assert spearman(positive) == Decimal("1.0")
    assert spearman(negative) == Decimal("-1.0")


def test_final_holdout_is_chronological_and_never_in_walk_forward_training() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {
            "execution_id": str(index),
            "fill_timestamp": (start + timedelta(hours=index * 5)).isoformat(),
            "exit_timestamp": (start + timedelta(hours=index * 5 + 1)).isoformat(),
        }
        for index in range(100)
    ]
    result = chronological_splits(rows)
    holdout = set(result["final_holdout_execution_ids"])
    assert len(holdout) == 20
    assert result["holdout_frozen_before_model_selection"] is True
    assert not any(
        holdout.intersection(fold["train_execution_ids"])
        or holdout.intersection(fold["validation_execution_ids"])
        for fold in result["folds"]
    )


def test_leave_one_run_out_persists_ids_without_held_run_leakage() -> None:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {
            "execution_id": str(index),
            "run_id": f"run-{index % 3}",
            "fill_timestamp": (start + timedelta(hours=index * 5)).isoformat(),
            "exit_timestamp": (start + timedelta(hours=index * 5 + 1)).isoformat(),
        }
        for index in range(60)
    ]
    result = chronological_splits(rows)
    development = {
        row["execution_id"]: row for row in rows
        if row["execution_id"] in result["development_execution_ids"]
    }
    for split in result["leave_one_run_out"]:
        assert all(
            development[execution_id]["run_id"] != split["held_out_run_id"]
            for execution_id in split["train_execution_ids"]
        )
        assert all(
            development[execution_id]["run_id"] == split["held_out_run_id"]
            for execution_id in split["validation_execution_ids"]
        )


def test_feature_manifest_forbids_post_entry_and_unknown_model_features() -> None:
    manifest = feature_manifest([{
        "execution_id": "one",
        "score": "0.7",
        "post__mfe_bps_60s": "12",
        "unproven_field": "abc",
    }])
    by_name = {item["field"]: item for item in manifest["features"]}
    assert by_name["score"]["availability"] == "PRE_ENTRY_AVAILABLE"
    assert by_name["post__mfe_bps_60s"]["availability"] == "POST_ENTRY_ONLY"
    assert by_name["post__mfe_bps_60s"]["used_for_candidate_model"] is False
    assert by_name["unproven_field"]["availability"] == "UNKNOWN"
    assert by_name["unproven_field"]["used_for_candidate_model"] is False


def test_isotonic_calibration_is_monotonic() -> None:
    rows = [
        {"score": str(index), "target": bool(index >= 10)}
        for index in range(20)
    ]
    model = fit_isotonic(rows, feature="score", target="target")
    assert model is not None
    predictions = [model.predict(row) for row in rows]
    assert predictions == sorted(predictions)
