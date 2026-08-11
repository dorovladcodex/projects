from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import inspect

import pytest

from app.config import Settings
from app.microstructure.calculations import (
    basis_bps,
    build_carry_candidate,
    build_leg_snapshot,
    estimate_taker_cost,
    evaluate_hypothetical_touch,
    hypothetical_quotes,
    synchronize_snapshot,
)
from app.microstructure.collector import (
    CollectorConfiguration,
    MicrostructureCollector,
)
from app.microstructure.models import HypotheticalQuote
from app.microstructure.public import BybitPublicReadOnlyClient, MicrostructureMarketState
from app.microstructure.storage import MicrostructureStorage


UTC = timezone.utc
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def leg(
    category: str,
    *,
    at: datetime = NOW,
    bid: str = "100",
    ask: str = "100.2",
    funding: str = "0.0001",
) -> object:
    ticker = {}
    if category == "linear":
        ticker = {
            "markPrice": "100.12",
            "indexPrice": "100.01",
            "fundingRate": funding,
            "nextFundingTime": str(int((at + timedelta(hours=8)).timestamp() * 1000)),
        }
    return build_leg_snapshot(
        category=category,
        symbol="BTCUSDT",
        exchange_timestamp=at,
        local_receive_timestamp=at + timedelta(milliseconds=10),
        bids=[(Decimal(bid), Decimal("20")), (Decimal("99"), Decimal("30"))],
        asks=[(Decimal(ask), Decimal("20")), (Decimal("101"), Decimal("30"))],
        recent_trade_price=Decimal("100.1"),
        recent_trade_volume=Decimal("0.5"),
        recent_trade_timestamp=at,
        ticker=ticker,
        funding_timestamp=at if category == "linear" else None,
        funding_interval_minutes=480 if category == "linear" else None,
        open_interest=Decimal("100000") if category == "linear" else None,
        open_interest_timestamp=at if category == "linear" else None,
    )


def snapshot(*, gap_ms: int = 0, complete_at: datetime | None = None):
    spot = leg("spot", at=NOW)
    perp = leg(
        "linear", at=NOW + timedelta(milliseconds=gap_ms),
        bid="100.1", ask="100.3",
    )
    return synchronize_snapshot(
        symbol="BTCUSDT",
        spot=spot,
        perpetual=perp,
        completed_at=complete_at or NOW + timedelta(seconds=1),
        clock_offset_ms=Decimal("2.5"),
        max_source_age_ms=Decimal("5000"),
        max_sync_gap_ms=Decimal("2000"),
    )


def test_spot_perp_timestamp_alignment_basis_and_funding_interval() -> None:
    row = snapshot(gap_ms=250)
    assert row.complete is True
    assert row.synchronization_gap_ms == Decimal("250.0")
    assert row.perpetual is not None
    assert row.perpetual.funding_interval_minutes == 480
    assert row.perpetual.current_funding_rate == Decimal("0.0001")
    assert row.perp_mid_vs_spot_mid_bps == basis_bps(
        row.spot.mid, row.perpetual.mid  # type: ignore[union-attr]
    )


def test_partial_data_and_alignment_fail_closed() -> None:
    row = snapshot(gap_ms=2501, complete_at=NOW + timedelta(seconds=4))
    assert row.complete is False
    assert "SPOT_PERP_SYNCHRONIZATION_GAP" in row.quality_reasons
    missing = synchronize_snapshot(
        symbol="BTCUSDT",
        spot=leg("spot"),
        perpetual=None,
        completed_at=NOW + timedelta(seconds=1),
        clock_offset_ms=None,
        max_source_age_ms=Decimal("5000"),
        max_sync_gap_ms=Decimal("2000"),
    )
    assert missing.complete is False
    assert missing.perp_mid_vs_spot_mid_bps is None
    assert "PERP_BOOK_MISSING" in missing.quality_reasons


def test_positive_server_clock_offset_preserves_exact_timestamps() -> None:
    received = NOW
    exchange = NOW + timedelta(milliseconds=600)
    skewed = build_leg_snapshot(
        category="spot", symbol="BTCUSDT",
        exchange_timestamp=exchange, local_receive_timestamp=received,
        bids=[(Decimal("100"), Decimal("1"))],
        asks=[(Decimal("101"), Decimal("1"))],
    )
    row = synchronize_snapshot(
        symbol="BTCUSDT", spot=skewed, perpetual=None,
        completed_at=NOW + timedelta(milliseconds=100),
        clock_offset_ms=Decimal("600"),
        max_source_age_ms=Decimal("5000"), max_sync_gap_ms=Decimal("2000"),
    )
    assert row.spot.exchange_timestamp == exchange
    assert row.spot.local_receive_timestamp == received
    assert row.spot_age_ms == Decimal("100.0")


def test_taker_cost_uses_depth_vwap_and_does_not_scale_linearly() -> None:
    market = build_leg_snapshot(
        category="spot",
        symbol="BTCUSDT",
        exchange_timestamp=NOW,
        local_receive_timestamp=NOW,
        bids=[(Decimal("99"), Decimal("100"))],
        asks=[(Decimal("100"), Decimal("1")), (Decimal("110"), Decimal("10"))],
    )
    small = estimate_taker_cost(
        capture_id="c", symbol="BTCUSDT", venue_leg="spot", side="BUY",
        notional_usdt=Decimal("100"), leg=market, fee_bps=Decimal("1"),
    )
    large = estimate_taker_cost(
        capture_id="c", symbol="BTCUSDT", venue_leg="spot", side="BUY",
        notional_usdt=Decimal("500"), leg=market, fee_bps=Decimal("1"),
    )
    assert small.sufficient_depth and large.sufficient_depth
    assert small.vwap == Decimal("100")
    assert large.vwap > small.vwap
    assert large.slippage_bps > small.slippage_bps
    unknown = estimate_taker_cost(
        capture_id="c", symbol="BTCUSDT", venue_leg="spot", side="SELL",
        notional_usdt=Decimal("100"), leg=market, fee_bps=None,
    )
    assert unknown.estimated_effective_cost_bps is None
    assert "ACCOUNT_FEE_UNKNOWN" in unknown.blockers


def test_funding_sign_handling_and_fee_unknown_are_explicit() -> None:
    positive, costs = build_carry_candidate(
        snapshot(),
        notionals=(Decimal("100"), Decimal("200"), Decimal("500"), Decimal("1000")),
        account_fees_bps={
            "spot_maker": None, "spot_taker": None,
            "perp_maker": None, "perp_taker": None,
        },
    )
    assert positive.classification == "POSITIVE_FUNDING_CARRY"
    assert positive.structure == "LONG_SPOT_SHORT_PERPETUAL"
    assert len(costs) == 16
    assert "ACCOUNT_FEE_CONFIGURATION_UNKNOWN" in positive.blockers
    negative_snapshot = synchronize_snapshot(
        symbol="BTCUSDT", spot=leg("spot"),
        perpetual=leg("linear", funding="-0.0001"),
        completed_at=NOW + timedelta(seconds=1), clock_offset_ms=Decimal("0"),
        max_source_age_ms=Decimal("5000"), max_sync_gap_ms=Decimal("2000"),
    )
    negative, _ = build_carry_candidate(
        negative_snapshot,
        notionals=(Decimal("100"),),
        account_fees_bps={
            "spot_maker": None, "spot_taker": None,
            "perp_maker": None, "perp_taker": None,
        },
    )
    assert negative.classification == "UNSUPPORTED_REVERSE_CARRY"
    assert "SPOT_SHORT_BORROW_MECHANICS_UNAVAILABLE" in negative.blockers


def test_hypothetical_touch_and_direction_adjusted_markout_never_claim_fill() -> None:
    quote = HypotheticalQuote(
        quote_id="q", capture_id="c", symbol="BTCUSDT", venue_leg="spot",
        side="BUY", quote_price=Decimal("100"), quote_time=NOW,
        best_bid=Decimal("100"), best_ask=Decimal("100.2"),
        spread_bps=Decimal("19.98"),
    )
    result = evaluate_hypothetical_touch(
        quote,
        horizon_seconds=1,
        evaluated_at=NOW + timedelta(seconds=3),
        trades=[(NOW + timedelta(milliseconds=500), Decimal("99.9"))],
        midpoints=[(NOW + timedelta(seconds=1.5), Decimal("101"))],
    )
    assert result.would_touch is True
    assert result.estimated_time_to_touch_seconds == Decimal("0.5")
    assert result.markout_bps == Decimal("100.00")
    assert result.terminology == "HYPOTHETICAL_TOUCH"
    assert result.actual_fill_claimed is False
    no_evidence = evaluate_hypothetical_touch(
        quote,
        horizon_seconds=1,
        evaluated_at=NOW + timedelta(seconds=3),
        trades=[],
        midpoints=[],
    )
    assert no_evidence.would_touch is False
    assert no_evidence.complete is False
    assert all(row.submitted is False for row in hypothetical_quotes(snapshot()))
    with pytest.raises(ValueError):
        quote.model_copy(update={"terminology": "FILL"}).model_validate(
            {**quote.model_dump(), "terminology": "FILL"}
        )


def test_public_client_is_get_only_allowlisted_and_has_no_order_method() -> None:
    calls: list[str] = []

    def fake_get(url: str, params: dict[str, str], timeout: float):
        calls.append(url)
        return {"retCode": 0, "time": str(int(NOW.timestamp() * 1000)), "result": {}}

    client = BybitPublicReadOnlyClient(http_get=fake_get)
    client.get("/v5/market/time")
    assert calls and client.exchange_mutation_capable is False
    with pytest.raises(ValueError):
        client.get("/v5/order/create")
    for name in ("place_order", "create_order", "cancel_order", "amend_order"):
        assert not hasattr(client, name)


def test_storage_restart_and_duplicate_protection(tmp_path: Path) -> None:
    storage = MicrostructureStorage(tmp_path)
    row = snapshot()
    assert storage.save_capture(row) is True
    assert storage.save_capture(row) is False
    for quote in hypothetical_quotes(row):
        assert storage.save_quote(quote) is True
        assert storage.save_quote(quote) is False
    storage.set_state("restart", {"count": 1})
    storage.close()
    reopened = MicrostructureStorage(tmp_path)
    assert reopened.get_state("restart") == {"count": 1}
    assert reopened.data_quality()["captures_total"] == 1
    assert reopened.save_capture(row) is False
    reopened.close()


class FakePublicClient:
    exchange_mutation_capable = False

    def __init__(self) -> None:
        self.at = datetime.now(UTC) - timedelta(seconds=1)

    def tickers(self, category: str):
        rows = []
        for index, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT")):
            rows.append({
                "symbol": symbol,
                "turnover24h": str(100_000_000 - index * 1_000_000),
                "bid1Price": "100",
                "ask1Price": "100.01",
                "markPrice": "100.005",
                "indexPrice": "100.004",
                "fundingRate": "0.0001",
                "nextFundingTime": str(int((NOW + timedelta(hours=8)).timestamp() * 1000)),
            })
        return rows, self.at

    def instrument(self, category: str, symbol: str):
        return {"fundingInterval": "480"}

    def orderbook(self, category: str, symbol: str):
        return {
            "b": [["100", "100"]], "a": [["100.01", "100"]],
            "cts": str(int(self.at.timestamp() * 1000)), "u": "1", "seq": "1",
        }

    def open_interest(self, symbol: str):
        return Decimal("100000"), self.at

    def clock_offset_ms(self):
        return Decimal("3"), Decimal("12")

    def funding_history(self, symbol: str, *, limit: int = 10):
        return [{
            "symbol": symbol,
            "fundingRate": "0.0001",
            "fundingRateTimestamp": str(int((NOW - timedelta(hours=8)).timestamp() * 1000)),
        }]


def collector_config(tmp_path: Path) -> CollectorConfiguration:
    settings = Settings(_env_file=None)
    config = CollectorConfiguration.from_settings(settings, Path.cwd())
    return replace(
        config,
        artifact_dir=tmp_path,
        universe_size=2,
        candidate_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT"),
        minimum_leg_turnover_usdt=Decimal("1000"),
    )


def test_collector_clock_offset_universe_capture_and_no_capacity_access(
    tmp_path: Path,
) -> None:
    collector = MicrostructureCollector(
        collector_config(tmp_path), client=FakePublicClient(),
        state=MicrostructureMarketState(),
    )
    initialized = collector.initialize()
    assert initialized["symbols"] == ("BTCUSDT", "ETHUSDT")
    assert collector.clock_offset_ms == Decimal("3")
    counts = collector.capture_once(completed_at=datetime.now(UTC))
    assert counts["captures"] == 2
    assert counts["complete"] == 2
    assert counts["costs"] == 32
    assert counts["quotes"] == 8
    assert collector.refresh_funding_events()["inserted"] == 2
    assert collector.storage.funding_summary()["funding_events"] == 2
    assert not hasattr(collector, "portfolio")
    assert not hasattr(collector, "capacity")
    assert not hasattr(collector, "risk")
    source = inspect.getsource(MicrostructureCollector)
    assert "create_order" not in source
    assert "place_order" not in source
    collector.close()


def test_future_label_query_never_reads_after_horizon(tmp_path: Path) -> None:
    storage = MicrostructureStorage(tmp_path)
    before = snapshot(complete_at=NOW + timedelta(hours=1) - timedelta(seconds=1))
    after = snapshot(complete_at=NOW + timedelta(hours=1, seconds=1))
    storage.save_capture(before)
    storage.save_capture(after)
    causal = storage.captures_between(
        "BTCUSDT", NOW, NOW + timedelta(hours=1)
    )
    assert [row.capture_id for row in causal] == [before.capture_id]
    assert all(row.snapshot_completed_at <= NOW + timedelta(hours=1) for row in causal)
    storage.close()


def test_migration_0015_is_additive_and_has_defined_downgrade() -> None:
    path = Path("alembic/versions/20260811_0015_v4_alpha_research.py")
    text = path.read_text(encoding="utf-8")
    upgrade = text.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    downgrade = text.split("def downgrade()", 1)[1]
    assert 'revision = "20260811_0015"' in text
    assert 'down_revision = "20260715_0014"' in text
    assert "create_table" in upgrade
    assert "drop_table" not in upgrade
    assert "alter_column" not in upgrade
    assert "execute(" not in upgrade
    assert "drop_table" in downgrade
