from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.bybit.demo_accounting import calculate_fill_level_accounting
from app.models import Side, Symbol


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "demo_replay"
    / "xrp_funding_pnl_7ee8a7b7.json"
)


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _calculate(data=None):
    data = data or _fixture()
    return calculate_fill_level_accounting(
        symbol=Symbol.XRPUSDT,
        side=Side.BUY,
        entry_order_id=data["entry_order_id"],
        close_order_id=data["close_order_id"],
        entry_fills=data["entry_fills"],
        close_fills=data["close_fills"],
        transaction_log=data["transaction_log"],
        closed_pnl_rows=data["closed_pnl"],
    )


def test_real_xrp_incident_reconciles_exactly_with_funding() -> None:
    result = _calculate()
    assert result.gross_price_pnl == Decimal("-0.4887")
    assert result.entry_fees == Decimal("0.10994302")
    assert result.close_fees == Decimal("0.10967424")
    assert result.funding_pnl == Decimal("-0.00298764")
    assert result.calculated_net_pnl == Decimal("-0.71130490")
    assert result.authoritative_closed_pnl == Decimal("-0.71130490")
    assert result.mismatch == 0
    assert result.final


def test_multiple_entry_and_close_fills_are_summed_once() -> None:
    data = _fixture()
    entry = data["entry_fills"][0]
    close = data["close_fills"][0]
    data["entry_fills"] = [
        {**entry, "execId": "entry-a", "execQty": "100"},
        {**entry, "execId": "entry-b", "execQty": "81"},
    ]
    data["close_fills"] = [
        {**close, "execId": "close-a", "execQty": "80"},
        {**close, "execId": "close-b", "execQty": "101"},
    ]
    data["entry_fills"][0]["execFee"] = "0.06000000"
    data["entry_fills"][1]["execFee"] = "0.04994302"
    data["close_fills"][0]["execFee"] = "0.05000000"
    data["close_fills"][1]["execFee"] = "0.05967424"
    result = _calculate(data)
    assert result.entry_quantity == Decimal("181")
    assert result.close_quantity == Decimal("181")
    assert result.calculated_net_pnl == Decimal("-0.71130490")
    assert result.final


def test_duplicate_ws_and_rest_fill_evidence_is_idempotent() -> None:
    data = _fixture()
    data["entry_fills"].append(dict(data["entry_fills"][0]))
    data["close_fills"].append(dict(data["close_fills"][0]))
    assert _calculate(data).calculated_net_pnl == Decimal("-0.71130490")


def test_conflicting_duplicate_execution_identity_fails_closed() -> None:
    data = _fixture()
    data["entry_fills"].append({
        **data["entry_fills"][0], "execPrice": "1.1045"
    })
    with pytest.raises(ValueError, match="conflicting"):
        _calculate(data)


def test_late_fee_or_funding_evidence_keeps_mismatch_visible() -> None:
    data = _fixture()
    data["transaction_log"] = []
    result = _calculate(data)
    assert result.status == "INCOMPLETE"
    assert result.mismatch == Decimal("0.00298764")
    assert not result.final


def test_maker_and_taker_fee_values_use_exact_exchange_amounts() -> None:
    data = _fixture()
    data["entry_fills"][0]["isMaker"] = True
    data["close_fills"][0]["isMaker"] = False
    result = _calculate(data)
    assert result.entry_fees + result.close_fees == Decimal("0.21961726")


def test_non_usdt_fee_currency_cannot_finalize_usdt_accounting() -> None:
    data = _fixture()
    data["close_fills"][0]["feeCurrency"] = "XRP"
    assert _calculate(data).status == "INCOMPLETE"


def test_decimal_precision_survives_payload_round_trip() -> None:
    result = _calculate()
    restored = json.loads(json.dumps(result.payload()))
    assert Decimal(restored["calculated_net_pnl"]) == Decimal("-0.71130490")
    assert Decimal(restored["funding_pnl"]) == Decimal("-0.00298764")
