from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from app.config import Settings
from app.models import Symbol
from app.v2.models import UniverseState
from app.v2.universe import (
    BybitPublicUniverseClient,
    SymbolUniverseService,
    UniverseInspectionError,
)


def _public_payloads(
    *,
    instrument_updates: dict[str, Any] | None = None,
    lot_updates: dict[str, Any] | None = None,
    ticker_updates: dict[str, Any] | None = None,
    ticker_missing: bool = False,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    lot = {
        "minOrderQty": "0.001",
        "qtyStep": "0.001",
        "minNotionalValue": "5",
    }
    lot.update(lot_updates or {})
    instrument = {
        "symbol": "BTCUSDT",
        "status": "Trading",
        "contractType": "LinearPerpetual",
        "settleCoin": "USDT",
        "launchTime": "not-a-number",  # malformed optional metric is harmless
        "lotSizeFilter": lot,
        "priceFilter": {"tickSize": "0.1"},
        "leverageFilter": {
            "minLeverage": "1",
            "maxLeverage": "100",
            "leverageStep": "0.01",
        },
    }
    instrument.update(instrument_updates or {})
    ticker = {
        "symbol": "BTCUSDT",
        "bid1Price": "99.9",
        "ask1Price": "100.1",
        "turnover24h": "1e7",
    }
    ticker.update(ticker_updates or {})
    book_bids = bids if bids is not None else [["99.9", "100"]]
    book_asks = asks if asks is not None else [["100.1", "100"]]

    def get(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
        del params, timeout
        if url.endswith("/v5/market/instruments-info"):
            return {"retCode": 0, "retMsg": "OK", "result": {"list": [instrument]}}
        if url.endswith("/v5/market/tickers"):
            rows = [] if ticker_missing else [ticker]
            return {"retCode": 0, "retMsg": "OK", "result": {"list": rows}}
        if url.endswith("/v5/market/orderbook"):
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {"b": book_bids, "a": book_asks, "ts": now_ms},
            }
        raise AssertionError(f"unexpected URL: {url}")

    return get


@pytest.mark.parametrize("value", ["", None])
def test_missing_required_numeric_filter_has_exact_reason(value: object) -> None:
    client = BybitPublicUniverseClient(
        http_get=_public_payloads(lot_updates={"minOrderQty": value})
    )

    with pytest.raises(UniverseInspectionError) as exc:
        client.inspect_symbol(Symbol.BTCUSDT)

    assert exc.value.reason == "missing_filter_field: field=lotSizeFilter.minOrderQty"


def test_scientific_notation_and_malformed_optional_metric_are_safe() -> None:
    client = BybitPublicUniverseClient(
        http_get=_public_payloads(
            lot_updates={
                "minOrderQty": "1e-3",
                "qtyStep": "1E-3",
                "minNotionalValue": "5e0",
            },
            ticker_updates={"turnover24h": "1.25e7"},
            bids=[["9.99e1", "1e2"]],
            asks=[["1.001e2", "1e2"]],
        )
    )

    result = client.inspect_symbol(Symbol.BTCUSDT)

    assert result.min_order_qty == Decimal("0.001")
    assert result.qty_step == Decimal("0.001")
    assert result.turnover_24h == Decimal("1.25e7")
    assert result.launch_time is None


def test_invalid_numeric_field_names_field_and_sanitized_value() -> None:
    client = BybitPublicUniverseClient(
        http_get=_public_payloads(ticker_updates={"turnover24h": "not numeric"})
    )

    with pytest.raises(UniverseInspectionError) as exc:
        client.inspect_symbol(Symbol.BTCUSDT)

    assert exc.value.reason == (
        "invalid_numeric_field: field=ticker.turnover24h value=not numeric"
    )


def test_unavailable_demo_instrument_has_exact_reason() -> None:
    def unavailable(
        url: str, params: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        del url, timeout
        return {
            "retCode": 10001,
            "retMsg": "params error: symbol invalid",
            "result": {"list": []},
            "requested": params.get("symbol"),
        }

    service = SymbolUniverseService(
        Settings(
            _env_file=None,
            v2_universe_symbols=("PEPEUSDT",),
            v2_min_turnover_24h_usdt=Decimal("0"),
            v2_min_orderbook_depth_usdt=Decimal("0"),
        ),
        BybitPublicUniverseClient(http_get=unavailable),
    )

    status = service.refresh()[Symbol.PEPEUSDT]

    assert status.state == UniverseState.DATA_UNAVAILABLE
    assert status.reasons == [
        "instrument_not_available_on_demo: field=symbol value=PEPEUSDT "
        "detail=retCode=10001 retMsg=params error: symbol invalid"
    ]


def test_missing_ticker_has_exact_reason() -> None:
    client = BybitPublicUniverseClient(http_get=_public_payloads(ticker_missing=True))

    with pytest.raises(UniverseInspectionError) as exc:
        client.inspect_symbol(Symbol.BTCUSDT)

    assert exc.value.reason == (
        "ticker_not_available: field=symbol value=BTCUSDT detail=response has no rows"
    )


def test_zero_bid_and_ask_have_exact_reason() -> None:
    client = BybitPublicUniverseClient(
        http_get=_public_payloads(
            ticker_updates={"bid1Price": "0", "ask1Price": "0"},
            bids=[["0", "1"]],
            asks=[["0", "1"]],
        )
    )

    with pytest.raises(UniverseInspectionError) as exc:
        client.inspect_symbol(Symbol.BTCUSDT)

    assert exc.value.code == "empty_bid_ask"
    assert "bid=0" in exc.value.reason
    assert "ask=0" in exc.value.reason


def test_one_bad_symbol_does_not_block_universe() -> None:
    good = _public_payloads()

    def mixed(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
        if params["symbol"] == "TONUSDT" and url.endswith(
            "/v5/market/instruments-info"
        ):
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {"list": [{"symbol": "TONUSDT", "status": "Closed"}]},
            }
        return good(url, params, timeout)

    service = SymbolUniverseService(
        Settings(
            _env_file=None,
            v2_universe_symbols=("BTCUSDT", "TONUSDT"),
            v2_min_turnover_24h_usdt=Decimal("1"),
            v2_min_orderbook_depth_usdt=Decimal("1"),
            v2_max_spread_bps=Decimal("100"),
        ),
        BybitPublicUniverseClient(http_get=mixed),
    )

    statuses = service.refresh()

    assert statuses[Symbol.BTCUSDT].accepted is True
    assert statuses[Symbol.TONUSDT].accepted is False
    assert statuses[Symbol.TONUSDT].reasons == [
        "instrument_not_available_on_demo: field=status value=Closed "
        "detail=symbol=TONUSDT"
    ]


def test_liquidity_rejections_use_structured_reason_codes() -> None:
    service = SymbolUniverseService(
        Settings(
            _env_file=None,
            v2_universe_symbols=("BTCUSDT",),
            v2_min_turnover_24h_usdt=Decimal("20000000"),
            v2_min_orderbook_depth_usdt=Decimal("20000"),
            v2_max_spread_bps=Decimal("100"),
        ),
        BybitPublicUniverseClient(http_get=_public_payloads()),
    )

    reasons = service.refresh()[Symbol.BTCUSDT].reasons

    assert any(reason.startswith("insufficient_turnover:") for reason in reasons)
    assert any(reason.startswith("insufficient_order_book:") for reason in reasons)

