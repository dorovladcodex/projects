from decimal import Decimal

import pytest

from app.bybit.demo import (
    CanaryMinimumOrderPlan,
    DemoSafetyError,
    InstrumentRules,
    calculate_minimum_valid_canary_order,
    revalidate_canary_order_plan,
)
from app.models import Symbol


def rules(**changes: object) -> InstrumentRules:
    values: dict[str, object] = {
        "symbol": Symbol.BTCUSDT,
        "status": "Trading",
        "qty_step": Decimal("0.001"),
        "min_order_qty": Decimal("0.001"),
        "min_notional_value": Decimal("5"),
        "tick_size": Decimal("0.10"),
        "min_leverage": Decimal("1"),
        "max_leverage": Decimal("100"),
        "leverage_step": Decimal("0.01"),
    }
    values.update(changes)
    return InstrumentRules(**values)  # type: ignore[arg-type]


def test_twenty_usdt_budget_fails_for_current_btc_like_minimum() -> None:
    with pytest.raises(DemoSafetyError, match="explicit maximum canary budget"):
        calculate_minimum_valid_canary_order(
            rules(), Decimal("62800"), Decimal("20")
        )


def test_seventy_five_usdt_budget_allows_exchange_minimum_not_full_budget() -> None:
    plan = calculate_minimum_valid_canary_order(
        rules(), Decimal("62800"), Decimal("75")
    )

    assert isinstance(plan, CanaryMinimumOrderPlan)
    assert plan.calculated_order_qty == Decimal("0.001")
    assert plan.estimated_notional == Decimal("62.800")
    assert plan.buffered_required_notional == Decimal("65.94000")
    assert plan.calculated_order_qty != Decimal("75") / plan.reference_price


def test_minimum_quantity_is_rounded_up_to_quantity_step() -> None:
    plan = calculate_minimum_valid_canary_order(
        rules(
            min_order_qty=Decimal("0.0011"),
            qty_step=Decimal("0.001"),
            min_notional_value=Decimal("0.1"),
        ),
        Decimal("100"),
        Decimal("1"),
        safety_buffer_pct=Decimal("0"),
    )

    assert plan.calculated_order_qty == Decimal("0.002")


def test_minimum_notional_can_determine_quantity() -> None:
    plan = calculate_minimum_valid_canary_order(
        rules(min_notional_value=Decimal("25")),
        Decimal("10000"),
        Decimal("40"),
    )

    assert plan.calculated_order_qty == Decimal("0.003")
    assert plan.estimated_notional == Decimal("30.000")


def test_rule_change_is_rejected_during_final_revalidation() -> None:
    plan = calculate_minimum_valid_canary_order(
        rules(), Decimal("62800"), Decimal("75")
    )

    with pytest.raises(DemoSafetyError, match="instrument rules changed"):
        revalidate_canary_order_plan(
            plan,
            rules(min_order_qty=Decimal("0.002")),
            Decimal("62800"),
        )


def test_price_change_beyond_budget_is_rejected() -> None:
    plan = calculate_minimum_valid_canary_order(
        rules(), Decimal("62800"), Decimal("75")
    )

    with pytest.raises(DemoSafetyError, match="explicit maximum canary budget"):
        revalidate_canary_order_plan(plan, rules(), Decimal("72000"))


def test_non_trading_symbol_is_rejected() -> None:
    with pytest.raises(DemoSafetyError, match="is not Trading"):
        calculate_minimum_valid_canary_order(
            rules(status="Settling"), Decimal("62800"), Decimal("75")
        )


def test_canary_calculation_rejects_float_financial_inputs() -> None:
    with pytest.raises(TypeError, match="must use Decimal"):
        calculate_minimum_valid_canary_order(
            rules(), 62800.0, Decimal("75")  # type: ignore[arg-type]
        )
