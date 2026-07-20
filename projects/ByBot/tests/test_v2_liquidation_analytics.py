from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.v2.analytics import (
    V2ReportGenerator,
    _liquidation_age_blockers,
    _liquidation_metrics_at,
)


def _row(stamp: datetime | None, age: float | None, *, state: str = "INELIGIBLE"):
    return {
        "last_valid_timestamp": stamp.isoformat() if stamp else None,
        "current_age_seconds": age,
        "state": state,
        "not_applicable_reason": (
            "liquidation_feed_stale" if stamp
            else "liquidation_feed_never_initialized"
        ),
    }


def _summary(
    generated_at: datetime,
    liquidation: dict,
    *, source_age: float | None = None,
) -> dict:
    runtime = {
        "news_source_metrics": {},
        "liquidation_metrics": liquidation,
        "stale_metrics": {
            "data_age_seconds_by_source": {
                "liquidations": {"latest_message_age": source_age}
            }
        },
    }
    return V2ReportGenerator._summary(
        "liquidation-test", [], [], [], [], [], runtime, [], generated_at
    )


def test_two_symbols_may_share_one_valid_liquidation_timestamp() -> None:
    generated = datetime(2026, 7, 17, 18, 4, 8, 802857, tzinfo=timezone.utc)
    stamp = datetime(2026, 7, 17, 18, 0, 6, 980000, tzinfo=timezone.utc)
    metrics, blockers = _liquidation_metrics_at({
        "liquidation_eligibility_by_symbol": {
            "BTCUSDT": _row(stamp, 200),
            "ETHUSDT": _row(stamp, 200),
        }
    }, generated)
    assert blockers == []
    assert metrics["liquidation_eligibility_by_symbol"]["BTCUSDT"]["current_age_seconds"] == pytest.approx(241.822857)
    assert metrics["liquidation_eligibility_by_symbol"]["ETHUSDT"]["current_age_seconds"] == pytest.approx(241.822857)


def test_newer_generic_source_message_does_not_replace_symbol_timestamp() -> None:
    generated = datetime.now(timezone.utc)
    stamp = generated - timedelta(seconds=240)
    report = _summary(
        generated,
        {"liquidation_eligibility_by_symbol": {"BTCUSDT": _row(stamp, 230)}},
        source_age=5,
    )
    assert report["analytics_result"] == "PASS"
    assert report["liquidation_eligibility"]["most_recent_age_seconds"] == pytest.approx(240)


def test_source_and_symbol_ages_may_differ_without_failure() -> None:
    generated = datetime.now(timezone.utc)
    stamp = generated - timedelta(seconds=120)
    report = _summary(
        generated,
        {"liquidation_eligibility_by_symbol": {"ETHUSDT": _row(stamp, 110)}},
        source_age=12.5,
    )
    assert report["analytics_blockers"] == []


def test_correct_per_symbol_age_passes_validation() -> None:
    generated = datetime.now(timezone.utc)
    metrics = {
        "liquidation_eligibility_by_symbol": {
            "BTCUSDT": _row(generated - timedelta(seconds=15), 15)
        },
        "most_recent_valid_liquidation_timestamp": (generated - timedelta(seconds=15)).isoformat(),
        "most_recent_age_seconds": 15,
        "oldest_valid_liquidation_timestamp": (generated - timedelta(seconds=15)).isoformat(),
        "maximum_age_seconds": 15,
    }
    assert _liquidation_age_blockers(metrics, generated) == []


def test_incorrect_per_symbol_age_fails_validation() -> None:
    generated = datetime.now(timezone.utc)
    metrics = {
        "liquidation_eligibility_by_symbol": {
            "BTCUSDT": _row(generated - timedelta(seconds=15), 14.98)
        },
    }
    assert _liquidation_age_blockers(metrics, generated) == [
        "liquidation timestamp/age mismatch: BTCUSDT"
    ]


def test_correct_most_recent_aggregate_age_passes() -> None:
    generated = datetime.now(timezone.utc)
    stamp = generated - timedelta(seconds=20)
    metrics = {
        "liquidation_eligibility_by_symbol": {},
        "most_recent_valid_liquidation_timestamp": stamp.isoformat(),
        "most_recent_age_seconds": 20,
    }
    assert _liquidation_age_blockers(metrics, generated) == []


def test_correct_oldest_and_maximum_aggregate_age_passes() -> None:
    generated = datetime.now(timezone.utc)
    stamp = generated - timedelta(seconds=80)
    metrics = {
        "liquidation_eligibility_by_symbol": {},
        "oldest_valid_liquidation_timestamp": stamp.isoformat(),
        "maximum_age_seconds": 80,
    }
    assert _liquidation_age_blockers(metrics, generated) == []


def test_null_timestamp_requires_null_age_and_compatible_state() -> None:
    generated = datetime.now(timezone.utc)
    valid = {
        "liquidation_eligibility_by_symbol": {"SOLUSDT": _row(None, None)}
    }
    assert _liquidation_age_blockers(valid, generated) == []
    invalid = {
        "liquidation_eligibility_by_symbol": {
            "SOLUSDT": _row(None, 1, state="ELIGIBLE")
        }
    }
    blockers = _liquidation_age_blockers(invalid, generated)
    assert "liquidation null timestamp has non-null age: SOLUSDT" in blockers
    assert "liquidation uninitialized symbol is not ineligible: SOLUSDT" in blockers


def test_truly_contradictory_pair_makes_analytics_fail() -> None:
    generated = datetime.now(timezone.utc)
    stamp = generated - timedelta(seconds=30)
    report = _summary(generated, {
        "age_calculated_at": generated.isoformat(),
        "liquidation_eligibility_by_symbol": {
            "BTCUSDT": _row(stamp, 1)
        },
        "most_recent_valid_liquidation_timestamp": stamp.isoformat(),
        "most_recent_age_seconds": 30,
        "oldest_valid_liquidation_timestamp": stamp.isoformat(),
        "maximum_age_seconds": 30,
    })
    assert report["analytics_result"] == "FAIL"
    assert "liquidation timestamp/age mismatch: BTCUSDT" in report["analytics_blockers"]


def test_exact_historical_btc_eth_example_is_analytics_pass() -> None:
    generated = datetime.fromisoformat("2026-07-17T18:04:08.802857+00:00")
    stamp = datetime.fromisoformat("2026-07-17T18:00:06.980000+00:00")
    report = _summary(generated, {
        "liquidation_eligibility_by_symbol": {
            "BTCUSDT": _row(stamp, 241.822857),
            "ETHUSDT": _row(stamp, 241.822857),
        },
        "most_recent_valid_liquidation_timestamp": stamp.isoformat(),
        "most_recent_age_seconds": 241.822857,
        "oldest_valid_liquidation_timestamp": stamp.isoformat(),
        "maximum_age_seconds": 241.822857,
    }, source_age=231.616044)
    assert report["analytics_result"] == "PASS"
    assert report["analytics_blockers"] == []

