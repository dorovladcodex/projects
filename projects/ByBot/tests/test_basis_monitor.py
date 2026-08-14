from __future__ import annotations

from typing import Any

import pytest

from app.basis.models import BasisObservation, Quote
from app.basis.monitor import BasisMonitor, curve_alerts
from app.history.client import BybitHistoryClient

DAY_MS = 86_400_000
NOW = 1_700_000_000_000


def quote(symbol: str, mid: float, spread: float = 1.0, depth: float = 50_000.0) -> Quote:
    return Quote(symbol=symbol, mid=mid, spread_bps=spread, depth_usd=depth)


def observation(
    *, days: float = 100.0, future_mid: float = 10_200.0, reference_mid: float = 10_000.0,
    reference_kind: str = "perp", future_depth: float = 50_000.0,
    reference_depth: float = 90_000.0,
) -> BasisObservation:
    return BasisObservation(
        observed_at_ms=NOW,
        base_coin="BTC",
        future=quote("BTCUSDT-01JAN27", future_mid, depth=future_depth),
        reference=quote("BTCUSDT", reference_mid, depth=reference_depth),
        reference_kind=reference_kind,
        delivery_ms=NOW + int(days * DAY_MS),
    )


# ------------------------------------------------------------------- pricing


def test_basis_and_annualisation() -> None:
    item = observation(days=365.0, future_mid=10_200.0, reference_mid=10_000.0)

    assert item.basis_bps == pytest.approx(200.0)
    assert item.annualised_bps == pytest.approx(200.0)


def test_shorter_tenor_annualises_higher_for_the_same_premium() -> None:
    long_dated = observation(days=365.0)
    short_dated = observation(days=90.0)

    assert short_dated.annualised_bps > long_dated.annualised_bps


def test_perp_reference_avoids_the_spot_fee() -> None:
    """The reason to quote against the perpetual rather than spot."""
    against_perp = observation(reference_kind="perp")
    against_spot = observation(reference_kind="spot")

    assert against_perp.round_trip_bps() < against_spot.round_trip_bps()
    # Spot costs 10 bps a side against 5.5, twice over.
    assert against_spot.round_trip_bps() - against_perp.round_trip_bps() == pytest.approx(9.0)


def test_round_trip_includes_both_quoted_spreads() -> None:
    item = BasisObservation(
        observed_at_ms=NOW, base_coin="BTC",
        future=quote("F", 10_200.0, spread=8.0),
        reference=quote("R", 10_000.0, spread=2.0),
        reference_kind="perp", delivery_ms=NOW + 100 * DAY_MS,
    )
    # 2x5.5 fee both legs = 22, plus 8 + 2 spreads, plus 2.5 slippage.
    assert item.round_trip_bps() == pytest.approx(34.5)


def test_maker_pricing_is_cheaper_than_taker() -> None:
    item = observation()
    assert item.round_trip_bps(maker=True) < item.round_trip_bps()


def test_costs_can_make_a_positive_basis_negative_net() -> None:
    """A near-dated contract cannot amortise a fixed round trip."""
    item = observation(days=5.0, future_mid=10_010.0)

    assert item.basis_bps > 0
    assert item.net_annualised_bps() < 0


def test_longer_tenor_amortises_the_same_cost_better() -> None:
    near = observation(days=30.0, future_mid=10_040.0)
    far = observation(days=300.0, future_mid=10_400.0)

    assert far.net_annualised_bps() > near.net_annualised_bps()


def test_capacity_is_bound_by_the_thinner_leg() -> None:
    item = observation(future_depth=12_000.0, reference_depth=900_000.0)
    assert item.capacity_usd == pytest.approx(12_000.0)


def test_expired_contract_is_not_tradeable() -> None:
    item = observation(days=0.0)
    assert item.tradeable is False
    assert item.annualised_bps == 0.0
    assert item.net_annualised_bps() == 0.0


def test_zero_reference_price_does_not_divide_by_zero() -> None:
    item = observation(reference_mid=0.0)
    assert item.basis_bps == 0.0


# -------------------------------------------------------------------- alerts


def test_alert_fires_when_a_contract_leaves_its_own_range() -> None:
    history = {"BTCUSDT-01JAN27": [440.0] * 10}
    item = observation(days=365.0, future_mid=10_600.0)  # 600 bps annualised

    alerts = curve_alerts(history, [item], threshold_bps=100.0)

    assert len(alerts) == 1
    assert alerts[0].direction == "rich"
    assert "rich" in alerts[0].describe()


def test_alert_direction_is_cheap_when_below_the_median() -> None:
    history = {"BTCUSDT-01JAN27": [440.0] * 10}
    item = observation(days=365.0, future_mid=10_100.0)  # 100 bps annualised

    alerts = curve_alerts(history, [item], threshold_bps=100.0)

    assert alerts[0].direction == "cheap"


def test_no_alert_within_the_threshold() -> None:
    history = {"BTCUSDT-01JAN27": [440.0] * 10}
    item = observation(days=365.0, future_mid=10_450.0)

    assert curve_alerts(history, [item], threshold_bps=100.0) == []


def test_thin_history_never_alerts() -> None:
    """A range needs observations before it means anything."""
    history = {"BTCUSDT-01JAN27": [440.0, 441.0]}
    item = observation(days=365.0, future_mid=11_000.0)

    assert curve_alerts(history, [item], minimum_observations=8) == []


def test_alerts_are_ranked_by_deviation() -> None:
    history = {"A": [440.0] * 10, "B": [440.0] * 10}
    small = BasisObservation(
        observed_at_ms=NOW, base_coin="BTC", future=quote("A", 10_600.0),
        reference=quote("R", 10_000.0), reference_kind="perp",
        delivery_ms=NOW + 365 * DAY_MS,
    )
    large = BasisObservation(
        observed_at_ms=NOW, base_coin="BTC", future=quote("B", 11_500.0),
        reference=quote("R", 10_000.0), reference_kind="perp",
        delivery_ms=NOW + 365 * DAY_MS,
    )
    alerts = curve_alerts(history, [small, large], threshold_bps=100.0)

    assert [alert.symbol for alert in alerts] == ["B", "A"]


# ------------------------------------------------------------------- monitor


class FakeTransport:
    def __init__(self, books: dict[str, Any], instruments: list[dict]) -> None:
        self.books = books
        self.instruments = instruments
        self.paths: list[str] = []

    def __call__(self, url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
        self.paths.append(url)
        if url.endswith("/instruments-info"):
            return {"retCode": 0, "result": {"list": self.instruments}}
        if url.endswith("/orderbook"):
            symbol = params["symbol"]
            book = self.books.get(symbol)
            if book is None:
                return {"retCode": 0, "result": {"b": [], "a": []}}
            return {"retCode": 0, "result": book}
        return {"retCode": 0, "result": {"list": []}}


def book(bid: float, ask: float, size: float = 10.0) -> dict[str, Any]:
    return {"b": [[str(bid), str(size)]], "a": [[str(ask), str(size)]]}


def monitor_with(instruments, books) -> BasisMonitor:
    transport = FakeTransport(books, instruments)
    client = BybitHistoryClient(
        "https://api.test", http_get=transport,
        min_request_interval_seconds=0.0, sleep=lambda _: None,
    )
    return BasisMonitor(client, now_ms=lambda: NOW)


def test_monitor_quotes_each_dated_contract_against_the_perp() -> None:
    instruments = [
        {"symbol": "BTCUSDT-01JAN27", "baseCoin": "BTC",
         "contractType": "LinearFutures", "deliveryTime": str(NOW + 100 * DAY_MS)},
        {"symbol": "BTCUSDT", "baseCoin": "BTC", "contractType": "LinearPerpetual"},
    ]
    books = {"BTCUSDT-01JAN27": book(10_190, 10_210), "BTCUSDT": book(9_995, 10_005)}

    result = monitor_with(instruments, books).observe()

    assert len(result) == 1
    assert result[0].reference.symbol == "BTCUSDT"
    assert result[0].basis_bps == pytest.approx(200.0, rel=1e-3)


def test_monitor_skips_already_delivered_contracts() -> None:
    instruments = [
        {"symbol": "BTCUSDT-OLD", "baseCoin": "BTC",
         "contractType": "LinearFutures", "deliveryTime": str(NOW - DAY_MS)},
    ]
    assert monitor_with(instruments, {}).observe() == []


def test_monitor_skips_a_contract_with_no_book() -> None:
    instruments = [
        {"symbol": "BTCUSDT-01JAN27", "baseCoin": "BTC",
         "contractType": "LinearFutures", "deliveryTime": str(NOW + 100 * DAY_MS)},
    ]
    books = {"BTCUSDT": book(9_995, 10_005)}  # future has no book
    assert monitor_with(instruments, books).observe() == []


def test_monitor_filters_by_base_coin() -> None:
    instruments = [
        {"symbol": "BTCUSDT-01JAN27", "baseCoin": "BTC",
         "contractType": "LinearFutures", "deliveryTime": str(NOW + 100 * DAY_MS)},
        {"symbol": "DOGEUSDT-01JAN27", "baseCoin": "DOGE",
         "contractType": "LinearFutures", "deliveryTime": str(NOW + 100 * DAY_MS)},
    ]
    books = {
        "BTCUSDT-01JAN27": book(10_190, 10_210), "BTCUSDT": book(9_995, 10_005),
        "DOGEUSDT-01JAN27": book(1.02, 1.03), "DOGEUSDT": book(1.0, 1.01),
    }
    result = monitor_with(instruments, books).observe(base_coins={"BTC"})

    assert [item.base_coin for item in result] == ["BTC"]


def test_monitor_reuses_one_reference_quote_per_underlying() -> None:
    instruments = [
        {"symbol": f"BTCUSDT-0{i}JAN27", "baseCoin": "BTC",
         "contractType": "LinearFutures", "deliveryTime": str(NOW + (100 + i) * DAY_MS)}
        for i in range(1, 4)
    ]
    books = {"BTCUSDT": book(9_995, 10_005)}
    books.update({row["symbol"]: book(10_190, 10_210) for row in instruments})
    transport = FakeTransport(books, instruments)
    client = BybitHistoryClient(
        "https://api.test", http_get=transport,
        min_request_interval_seconds=0.0, sleep=lambda _: None,
    )
    BasisMonitor(client, now_ms=lambda: NOW).observe()

    orderbook_calls = [p for p in transport.paths if p.endswith("/orderbook")]
    assert len(orderbook_calls) == 4, "three futures plus one shared perp quote"


def test_monitor_rejects_an_unknown_reference_kind() -> None:
    with pytest.raises(ValueError, match="unsupported reference"):
        monitor_with([], {}).observe(reference_kind="options")


def test_monitor_has_no_order_capability() -> None:
    monitor = monitor_with([], {})
    assert monitor.client.exchange_mutation_capable is False
    assert not any(
        name in dir(monitor) for name in ("place_order", "submit", "create_order")
    )
