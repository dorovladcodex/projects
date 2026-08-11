from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.history.client import (
    KLINE_PAGE_LIMIT,
    BybitHistoryClient,
    HistoryRequestError,
)
from app.history.models import (
    FundingRate,
    Kline,
    KlineInterval,
    OpenInterest,
    OpenInterestInterval,
)

MINUTE_MS = 60_000
# A realistic epoch: the models reject 0 so that a dropped timestamp cannot
# masquerade as a valid 1970 bar.
T0 = 1_700_000_000_000


def ok(rows: list[Any]) -> dict[str, Any]:
    return {"retCode": 0, "retMsg": "OK", "result": {"list": rows}}


def bar(start_ms: int, close: str = "100") -> list[str]:
    return [str(start_ms), "100", "101", "99", close, "12.5", "1250"]


class RecordingTransport:
    """Deterministic stand-in for the public HTTP layer."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
        self.calls.append((url, dict(params)))
        if not self.responses:
            return ok([])
        return self.responses.pop(0)


def client(transport: RecordingTransport, **kwargs: Any) -> BybitHistoryClient:
    return BybitHistoryClient(
        "https://api.test",
        http_get=transport,
        min_request_interval_seconds=0.0,
        sleep=lambda _: None,
        **kwargs,
    )


# ------------------------------------------------------------------ safety


def test_client_rejects_non_allowlisted_path() -> None:
    api = client(RecordingTransport([]))
    with pytest.raises(ValueError, match="not allowlisted"):
        api.get("/v5/order/create", {})


def test_client_exposes_no_mutation_capability() -> None:
    api = client(RecordingTransport([]))
    assert api.exchange_mutation_capable is False
    assert not any(
        name in dir(api) for name in ("place_order", "create_order", "cancel_order")
    )


def test_allowlist_contains_only_market_reads() -> None:
    assert all(path.startswith("/v5/market/") for path in BybitHistoryClient.ALLOWED_PATHS)


# ------------------------------------------------------------------ parsing


def test_kline_page_parses_and_sorts_ascending() -> None:
    transport = RecordingTransport([ok([bar(3 * MINUTE_MS), bar(MINUTE_MS), bar(2 * MINUTE_MS)])])
    bars = client(transport).kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, 10 * MINUTE_MS)

    assert [item.start_ms for item in bars] == [MINUTE_MS, 2 * MINUTE_MS, 3 * MINUTE_MS]
    assert bars[0].open == Decimal("100")
    assert bars[0].turnover == Decimal("1250")
    assert bars[0].interval is KlineInterval.ONE_MINUTE


def test_kline_page_rejects_short_row() -> None:
    transport = RecordingTransport([ok([["1", "2", "3"]])])
    with pytest.raises(HistoryRequestError, match="unexpected kline row shape"):
        client(transport).kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS)


def test_kline_page_rejects_unparsable_number_rather_than_zeroing() -> None:
    transport = RecordingTransport([ok([[str(MINUTE_MS), "abc", "1", "1", "1", "1", "1"]])])
    with pytest.raises(HistoryRequestError, match="unparsable numeric field"):
        client(transport).kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS)


def test_kline_page_rejects_empty_number_rather_than_zeroing() -> None:
    transport = RecordingTransport([ok([[str(MINUTE_MS), "100", "101", "99", "100", "", "1"]])])
    with pytest.raises(HistoryRequestError, match="missing numeric field"):
        client(transport).kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS)


def test_funding_page_preserves_negative_rate() -> None:
    transport = RecordingTransport(
        [ok([{"symbol": "BTCUSDT", "fundingRate": "-0.00031", "fundingRateTimestamp": "1700"}])]
    )
    rates = client(transport).funding_page("BTCUSDT", 0, 10_000)

    assert rates[0].funding_rate == Decimal("-0.00031")
    assert rates[0].funding_time_ms == 1700


def test_open_interest_page_parses() -> None:
    transport = RecordingTransport(
        [ok([{"openInterest": "51234.5", "timestamp": "1700"}])]
    )
    points = client(transport).open_interest_page(
        "BTCUSDT", OpenInterestInterval.FIVE_MINUTES, 0, 10_000
    )

    assert points[0].open_interest == Decimal("51234.5")
    assert points[0].interval is OpenInterestInterval.FIVE_MINUTES


def test_missing_result_is_an_error_not_an_empty_page() -> None:
    transport = RecordingTransport([{"retCode": 0, "retMsg": "OK"}])
    with pytest.raises(HistoryRequestError, match="missing result"):
        client(transport).kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS)


def test_null_list_is_an_empty_page() -> None:
    transport = RecordingTransport([{"retCode": 0, "result": {"list": None}}])
    assert client(transport).kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS) == []


# ----------------------------------------------------------------- retries


def test_rate_limit_is_retried_then_succeeds() -> None:
    transport = RecordingTransport(
        [{"retCode": 10006, "retMsg": "too many visits"}, ok([bar(MINUTE_MS)])]
    )
    bars = client(transport).kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS)

    assert len(bars) == 1
    assert len(transport.calls) == 2


def test_non_rate_limit_error_fails_immediately() -> None:
    transport = RecordingTransport([{"retCode": 10001, "retMsg": "params error"}])
    with pytest.raises(HistoryRequestError, match="retCode=10001"):
        client(transport).kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS)
    assert len(transport.calls) == 1


def test_transport_failure_is_retried_to_exhaustion() -> None:
    def failing(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
        raise TimeoutError("network down")

    api = BybitHistoryClient(
        "https://api.test", http_get=failing, min_request_interval_seconds=0.0,
        sleep=lambda _: None, max_attempts=3,
    )
    with pytest.raises(HistoryRequestError, match="after 3 attempts"):
        api.kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS)


def test_throttle_waits_between_requests() -> None:
    slept: list[float] = []
    now = [100.0]
    transport = RecordingTransport([ok([bar(MINUTE_MS)]), ok([bar(2 * MINUTE_MS)])])
    api = BybitHistoryClient(
        "https://api.test", http_get=transport, min_request_interval_seconds=0.5,
        sleep=slept.append, monotonic=lambda: now[0],
    )
    api.kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS)
    api.kline_page("BTCUSDT", KlineInterval.ONE_MINUTE, 0, MINUTE_MS)

    assert slept == [0.5]


# -------------------------------------------------------------- pagination


def test_iter_klines_advances_past_returned_data() -> None:
    window = KLINE_PAGE_LIMIT * MINUTE_MS
    transport = RecordingTransport(
        [ok([bar(T0), bar(T0 + MINUTE_MS)]), ok([bar(T0 + window)]), ok([])]
    )
    pages = list(
        client(transport).iter_klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + 3 * window)
    )

    assert [len(page) for page in pages] == [2, 1]
    assert transport.calls[1][1]["start"] == str(T0 + 2 * MINUTE_MS)


def test_iter_klines_skips_empty_window_without_truncating() -> None:
    """A gap must not be mistaken for the end of history."""
    window = KLINE_PAGE_LIMIT * MINUTE_MS
    transport = RecordingTransport([ok([]), ok([]), ok([bar(T0 + 2 * window)])])
    pages = list(
        client(transport).iter_klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + 3 * window)
    )

    assert len(pages) == 1
    assert pages[0][0].start_ms == T0 + 2 * window
    # Two empty windows, the window holding the bar, then the remainder of that
    # window after the bar: iteration is driven by the range, not by the first
    # empty response.
    assert len(transport.calls) == 4
    assert transport.calls[2][1]["start"] == str(T0 + 2 * window)


def test_iter_klines_stops_at_end_boundary() -> None:
    transport = RecordingTransport([ok([bar(T0)])])
    list(client(transport).iter_klines("BTCUSDT", KlineInterval.ONE_MINUTE, T0, T0 + MINUTE_MS))

    assert transport.calls[0][1]["end"] == str(T0 + MINUTE_MS)


def test_iter_funding_advances_by_last_row_on_full_page() -> None:
    full = [
        {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(i * 100 + 1)}
        for i in range(200)
    ]
    transport = RecordingTransport([ok(full), ok([])])
    pages = list(client(transport).iter_funding("BTCUSDT", 0, 200 * 3_600_000 * 2))

    assert len(pages) == 1
    assert transport.calls[1][1]["startTime"] == str(199 * 100 + 2)


def test_iter_open_interest_paginates() -> None:
    step = OpenInterestInterval.FIVE_MINUTES.milliseconds
    window = 200 * step
    transport = RecordingTransport([ok([{"openInterest": "1", "timestamp": "1"}]), ok([])])
    pages = list(
        client(transport).iter_open_interest(
            "BTCUSDT", OpenInterestInterval.FIVE_MINUTES, 0, 2 * window
        )
    )

    assert len(pages) == 1


# --------------------------------------------------------------- discovery


def test_earliest_kline_ms_returns_none_when_symbol_has_no_history() -> None:
    transport = RecordingTransport([])  # every response is an empty page
    assert (
        client(transport).earliest_kline_ms(
            "NEWUSDT", KlineInterval.ONE_MINUTE, floor_ms=0, ceiling_ms=10 * MINUTE_MS
        )
        is None
    )


def test_earliest_kline_ms_returns_floor_when_data_starts_at_floor() -> None:
    transport = RecordingTransport([ok([bar(MINUTE_MS)])])
    found = client(transport).earliest_kline_ms(
        "BTCUSDT", KlineInterval.ONE_MINUTE, floor_ms=0, ceiling_ms=10_000 * MINUTE_MS
    )

    assert found == 0


# ------------------------------------------------------------------ models


def test_kline_model_rejects_high_below_low() -> None:
    with pytest.raises(ValidationError, match="high < low"):
        Kline(
            symbol="BTCUSDT", interval=KlineInterval.ONE_MINUTE, start_ms=1,
            open=Decimal("100"), high=Decimal("90"), low=Decimal("95"),
            close=Decimal("96"), volume=Decimal("1"), turnover=Decimal("1"),
        )


def test_kline_model_rejects_close_outside_range() -> None:
    with pytest.raises(ValidationError, match="close outside range"):
        Kline(
            symbol="BTCUSDT", interval=KlineInterval.ONE_MINUTE, start_ms=1,
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
            close=Decimal("150"), volume=Decimal("1"), turnover=Decimal("1"),
        )


def test_kline_model_rejects_non_positive_price() -> None:
    with pytest.raises(ValidationError):
        Kline(
            symbol="BTCUSDT", interval=KlineInterval.ONE_MINUTE, start_ms=1,
            open=Decimal("0"), high=Decimal("1"), low=Decimal("0"),
            close=Decimal("1"), volume=Decimal("1"), turnover=Decimal("1"),
        )


def test_interval_milliseconds_are_consistent() -> None:
    assert KlineInterval.ONE_MINUTE.milliseconds == 60_000
    assert KlineInterval.ONE_DAY.milliseconds == 86_400_000
    assert OpenInterestInterval.FOUR_HOURS.milliseconds == 14_400_000


def test_timestamps_convert_to_utc() -> None:
    rate = FundingRate(symbol="BTCUSDT", funding_rate=Decimal("0"), funding_time_ms=1_700_000_000_000)
    assert rate.funding_at.tzinfo is not None
    assert rate.funding_at.year == 2023


def test_open_interest_rejects_negative_value() -> None:
    with pytest.raises(ValidationError):
        OpenInterest(
            symbol="BTCUSDT", interval=OpenInterestInterval.ONE_HOUR,
            timestamp_ms=1, open_interest=Decimal("-1"),
        )
