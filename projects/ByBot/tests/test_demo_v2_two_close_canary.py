from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import Symbol
from app.v2.models import (
    MarketFeatureSnapshot,
    UniverseInstrument,
    UniverseState,
    UniverseStatus,
)
from scripts.demo_v2_two_close_canary import (
    ControllerFailure,
    canary_portfolio_reasons,
    evaluate_feature_readiness,
    normalize_linear_category,
    select_canary_symbols,
    terminalization_pending_execution_ids,
    wait_execution,
)


NOW = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)


def instrument(symbol: Symbol) -> UniverseStatus:
    return UniverseStatus(
        symbol=symbol,
        state=UniverseState.ACCEPTED,
        accepted=True,
        instrument=UniverseInstrument(
            symbol=symbol,
            status="Trading",
            # This is the exact durable value emitted by the current Bybit
            # Demo universe adapter for USDT perpetual instruments.
            category="linearperpetual",
            settle_coin="USDT",
            min_order_qty=Decimal("1"),
            qty_step=Decimal("1"),
            min_notional_value=Decimal("5"),
            min_leverage=Decimal("1"),
            max_leverage=Decimal("50"),
            leverage_step=Decimal("0.01"),
            tick_size=Decimal("0.0001"),
            turnover_24h=Decimal("1000000"),
            spread_bps=Decimal("1"),
            bid_depth_usdt=Decimal("50000"),
            ask_depth_usdt=Decimal("50000"),
            market_timestamp=NOW,
        ),
        checked_at=NOW,
    )


def feature(symbol: Symbol, *, age_seconds: int = 1) -> dict:
    captured = NOW - timedelta(seconds=age_seconds)
    return MarketFeatureSnapshot(
        symbol=symbol,
        timestamp=captured,
        fresh=True,
        last_price=Decimal("0.60"),
        bid_price=Decimal("0.5999"),
        ask_price=Decimal("0.6001"),
        spread_bps=Decimal("3.33"),
        bid_depth_usdt=Decimal("50000"),
        ask_depth_usdt=Decimal("50000"),
        bid_depth_10bps_usdt=Decimal("5000"),
        ask_depth_10bps_usdt=Decimal("5000"),
        source_timestamps={
            "ticker": captured,
            "orderbook": captured,
        },
    ).model_dump(mode="json")


def test_default_selection_prefers_xrp_and_ada_and_avoids_btc() -> None:
    selected = select_canary_symbols(
        ["BTCUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT"]
    )
    assert selected[:2] == ["XRPUSDT", "ADAUSDT"]


@pytest.mark.parametrize(
    "category",
    ["linear", "LinearPerpetual", "linear_perpetual"],
)
def test_known_linear_contract_categories_are_normalized(
    category: str,
) -> None:
    assert normalize_linear_category(category) == "LINEAR_PERPETUAL"


@pytest.mark.parametrize(
    "category",
    ["spot", "inverse", "InversePerpetual", "option", "unknown", "linearly"],
)
def test_non_linear_contract_categories_are_rejected(category: str) -> None:
    assert normalize_linear_category(category) is None


def test_selection_uses_two_distinct_fallback_symbols() -> None:
    selected = select_canary_symbols(
        ["BTCUSDT", "AVAXUSDT", "LINKUSDT"]
    )
    assert selected[:2] == ["AVAXUSDT", "LINKUSDT"]


def test_explicit_unavailable_or_duplicate_symbols_fail_before_candidate() -> None:
    with pytest.raises(ControllerFailure, match="distinct accepted"):
        select_canary_symbols(
            ["XRPUSDT", "ADAUSDT"], ["XRPUSDT", "XRPUSDT"]
        )
    with pytest.raises(ControllerFailure, match="distinct accepted"):
        select_canary_symbols(
            ["XRPUSDT", "ADAUSDT"], ["XRPUSDT", "SOLUSDT"]
        )


def test_complete_fresh_production_feature_is_ready() -> None:
    result = evaluate_feature_readiness(
        symbol="XRPUSDT",
        universe_status=instrument(Symbol.XRPUSDT),
        payload=feature(Symbol.XRPUSDT),
        now=NOW,
        freshness_seconds=15,
    )
    assert result["ready"] is True
    assert result["source"] == "WS"
    assert result["raw_category"] == "linearperpetual"
    assert result["normalized_category"] == "LINEAR_PERPETUAL"
    assert result["age_seconds"] == 1
    assert result["missing_fields"] == []


def test_stale_feature_is_reported_and_never_ready() -> None:
    result = evaluate_feature_readiness(
        symbol="XRPUSDT",
        universe_status=instrument(Symbol.XRPUSDT),
        payload=feature(Symbol.XRPUSDT, age_seconds=16),
        now=NOW,
        freshness_seconds=15,
    )
    assert result["ready"] is False
    assert "feature.timestamp_fresh" in result["missing_fields"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ask_price", None),
        ("bid_depth_10bps_usdt", "0"),
        ("ask_depth_10bps_usdt", "NaN"),
    ],
)
def test_missing_zero_or_nan_required_feature_is_reported(
    field: str, value: object,
) -> None:
    payload = feature(Symbol.ADAUSDT)
    payload[field] = value
    result = evaluate_feature_readiness(
        symbol="ADAUSDT",
        universe_status=instrument(Symbol.ADAUSDT),
        payload=payload,
        now=NOW,
        freshness_seconds=15,
    )
    assert result["ready"] is False
    assert result["missing_fields"]


def test_missing_exchange_timestamp_is_reported() -> None:
    payload = feature(Symbol.XRPUSDT)
    payload["source_timestamps"]["orderbook"] = None
    result = evaluate_feature_readiness(
        symbol="XRPUSDT",
        universe_status=instrument(Symbol.XRPUSDT),
        payload=payload,
        now=NOW,
        freshness_seconds=15,
    )
    assert result["ready"] is False
    assert "source_timestamps.orderbook" in result["missing_fields"]


def test_canary_controller_exposes_bounded_feature_readiness_stage() -> None:
    source = (
        Path(__file__).parents[1]
        / "scripts"
        / "demo_v2_two_close_canary.py"
    ).read_text(encoding="utf-8")
    assert '"CANARY_MARKET_FEATURE_READY"' in source
    assert "timeout_seconds=45" in source
    assert "poll_seconds=0.35" in source
    assert "latest_feature_rows" in source
    assert "feature_snapshot is unavailable" not in source


def test_execution_poll_retries_one_transient_timeout(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_get_json(base_url: str, path: str, timeout: int = 10) -> dict:
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("transient cached-status timeout")
        return {"execution": {"state": "DEMO_CLOSED"}}

    monkeypatch.setattr(
        "scripts.demo_v2_two_close_canary.get_json", fake_get_json
    )

    result = wait_execution(
        "http://127.0.0.1:1", "execution-id", {"DEMO_CLOSED"}, 2
    )

    assert result["state"] == "DEMO_CLOSED"
    assert calls["count"] == 2


def test_pending_observation_counts_exact_execution_coverage_not_events() -> None:
    events = [
        {
            "event": "MONITOR_TERMINALIZATION_PENDING",
            "execution_ids": ["execution-1"],
        },
        {
            "event": "MONITOR_TERMINALIZATION_PENDING",
            "execution_ids": ["execution-1"],
            "resolved": True,
        },
        {
            "event": "MONITOR_TERMINALIZATION_PENDING",
            "execution_ids": ["execution-2"],
            "resolved": True,
        },
    ]

    assert terminalization_pending_execution_ids(events) == {
        "execution-1", "execution-2"
    }


def test_canary_waits_for_durable_symbol_cooldown_and_two_entry_slots() -> None:
    state = {
        "symbol_cooldowns": {
            "XRPUSDT": (NOW + timedelta(seconds=4)).isoformat(),
        },
        "reservations": [
            {
                "state": "RELEASED",
                "execution_id": f"execution-{index}",
                "created_at": (
                    NOW - timedelta(minutes=4, seconds=index)
                ).isoformat(),
            }
            for index in range(4)
        ],
    }

    reasons = canary_portfolio_reasons(
        state,
        symbol="XRPUSDT",
        now=NOW,
        max_new_entries_per_5_minutes=5,
        max_trades_per_day=100,
    )
    later = canary_portfolio_reasons(
        state,
        symbol="XRPUSDT",
        now=NOW + timedelta(minutes=6),
        max_new_entries_per_5_minutes=5,
        max_trades_per_day=100,
    )

    assert "symbol cooldown is active" in reasons
    assert "five-minute entry capacity is unavailable for two positions" in reasons
    assert later == []
