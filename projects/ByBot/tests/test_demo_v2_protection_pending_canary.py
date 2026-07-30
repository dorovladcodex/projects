from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models import DemoExecutionState, Side, Symbol
from scripts.demo_v2_protection_pending_canary import (
    ResidualOrderVerificationError,
    wait_for_authoritative_final_orders,
)


NOW = datetime(2026, 7, 30, 11, 17, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds

    def utcnow(self) -> datetime:
        return NOW


class FakeRepository:
    def __init__(self, *records) -> None:
        self.records = list(records)

    def load_demo_executions(self):
        return list(self.records)


class FakeReadClient:
    def __init__(self, orders, *, positions=None) -> None:
        self.order_snapshots = [list(item) for item in orders]
        self.position_snapshots = [
            list(item) for item in (positions or [[]])
        ]
        self.order_calls = 0
        self.position_calls = 0

    def get_open_orders(self):
        index = min(self.order_calls, len(self.order_snapshots) - 1)
        self.order_calls += 1
        return list(self.order_snapshots[index])

    def get_usdt_positions(self):
        index = min(self.position_calls, len(self.position_snapshots) - 1)
        self.position_calls += 1
        return list(self.position_snapshots[index])


def record():
    return SimpleNamespace(
        id="execution-id",
        state=DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
        symbol=Symbol.BTCUSDT,
        side=Side.BUY,
        order_id="entry-id",
        order_link_id="entry-link",
        close_order_id="close-id",
        close_order_link_id="close-link",
        tp_order_id="tp-id",
        sl_order_id="sl-id",
        protection_position_idx=0,
        accepted_quantity=Decimal("0.002"),
        closed_at=NOW,
        updated_at=NOW,
        terminalization_completed_at=NOW,
    )


def protection(order_id: str, stop_type: str) -> dict:
    return {
        "orderId": order_id,
        "orderLinkId": "",
        "symbol": "BTCUSDT",
        "side": "Sell",
        "orderStatus": "Untriggered",
        "cancelType": "",
        "reduceOnly": True,
        "closeOnTrigger": True,
        "positionIdx": 0,
        "stopOrderType": stop_type,
        "qty": "0.002",
    }


def run_wait(client, *, timeout=30.0, cached=1, records=None):
    clock = FakeClock()
    result = wait_for_authoritative_final_orders(
        execution_id="execution-id",
        cached_open_order_count=cached,
        repository=FakeRepository(*(records or [record()])),
        client=client,
        timeout_seconds=timeout,
        poll_seconds=0.75,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        utcnow=clock.utcnow,
    )
    return result, clock


def test_one_exact_residual_waits_until_authoritative_zero() -> None:
    result, clock = run_wait(
        FakeReadClient([[protection("tp-id", "TakeProfit")], []])
    )
    assert result["residual_orders_initial"] == 1
    assert result["residual_orders_exact_owned"] == 1
    assert result["residual_wait_attempts"] == 2
    assert result["final_authoritative_open_orders"] == 0
    assert result["residual_result"] == "PASS"
    assert clock.sleeps == [0.75]


def test_two_exact_tp_sl_residuals_are_pending_cancel() -> None:
    orders = [
        protection("tp-id", "TakeProfit"),
        protection("sl-id", "StopLoss"),
    ]
    result, _ = run_wait(FakeReadClient([orders, orders, []]))
    assert result["residual_orders_initial"] == 2
    assert result["residual_orders_exact_owned"] == 2
    assert result["residual_wait_attempts"] == 3
    assert result["residual_cancellation_timeline"][0][
        "classification"
    ] == "BOT_OWNED_PENDING_CANCEL"


def test_stale_cached_count_does_not_override_authoritative_zero() -> None:
    result, clock = run_wait(FakeReadClient([[]]), cached=2)
    assert result["cached_open_orders_initial"] == 2
    assert result["final_authoritative_open_orders"] == 0
    assert result["residual_wait_attempts"] == 1
    assert not clock.sleeps


def test_exact_residual_timeout_is_precise_and_bounded() -> None:
    client = FakeReadClient([[protection("tp-id", "TakeProfit")]])
    clock = FakeClock()
    with pytest.raises(
        ResidualOrderVerificationError,
        match="BOT_OWNED_TERMINAL_RESIDUAL_TIMEOUT",
    ) as raised:
        wait_for_authoritative_final_orders(
            execution_id="execution-id",
            cached_open_order_count=1,
            repository=FakeRepository(record()),
            client=client,
            timeout_seconds=1.5,
            poll_seconds=0.75,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            utcnow=clock.utcnow,
        )
    assert raised.value.result["residual_wait_ms"] == 1500
    assert raised.value.result["final_authoritative_open_orders"] == 1
    assert sum(clock.sleeps) == 1.5


def test_unrelated_order_fails_immediately_without_wait() -> None:
    manual = protection("manual-id", "TakeProfit")
    clock = FakeClock()
    with pytest.raises(
        ResidualOrderVerificationError, match="UNRELATED_EXTERNAL"
    ):
        wait_for_authoritative_final_orders(
            execution_id="execution-id",
            cached_open_order_count=1,
            repository=FakeRepository(record()),
            client=FakeReadClient([[manual]]),
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            utcnow=clock.utcnow,
        )
    assert not clock.sleeps


def test_duplicate_exact_order_id_fails_as_ownership_conflict() -> None:
    duplicate = protection("tp-id", "TakeProfit")
    with pytest.raises(
        ResidualOrderVerificationError, match="OWNERSHIP_CONFLICT"
    ):
        run_wait(FakeReadClient([[duplicate, dict(duplicate)]]))


def test_reused_exact_id_across_executions_fails_closed() -> None:
    other = record()
    other.id = "other-execution"
    other.tp_order_id = "tp-id"
    with pytest.raises(
        ResidualOrderVerificationError, match="OWNERSHIP_CONFLICT"
    ):
        run_wait(
            FakeReadClient([[protection("tp-id", "TakeProfit")]]),
            records=[record(), other],
        )


def test_reopened_position_fails_immediately() -> None:
    position = {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "size": "0.002",
        "positionIdx": 0,
    }
    with pytest.raises(
        ResidualOrderVerificationError, match="REMOTE_POSITION_REOPENED"
    ):
        run_wait(FakeReadClient([[]], positions=[[position]]))


def test_symbol_only_order_is_never_attributed() -> None:
    same_symbol = protection("unknown-id", "TakeProfit")
    with pytest.raises(
        ResidualOrderVerificationError, match="UNRELATED_EXTERNAL"
    ):
        run_wait(FakeReadClient([[same_symbol]]))


def test_contradictory_exact_order_metadata_is_conflict() -> None:
    wrong_side = protection("tp-id", "TakeProfit")
    wrong_side["side"] = "Buy"
    with pytest.raises(
        ResidualOrderVerificationError, match="OWNERSHIP_CONFLICT"
    ):
        run_wait(FakeReadClient([[wrong_side]]))


def test_second_real_canary_residual_replay_passes() -> None:
    residual = protection(
        "510c253c-bac4-4998-b687-93c108aba9d9",
        "TakeProfit",
    )
    incident = record()
    incident.id = "aa1fab1c-d96e-4366-b2da-c05b9f3fec78"
    incident.tp_order_id = residual["orderId"]
    clock = FakeClock()
    result = wait_for_authoritative_final_orders(
        execution_id=str(incident.id),
        cached_open_order_count=1,
        repository=FakeRepository(incident),
        client=FakeReadClient([[residual], []]),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        utcnow=clock.utcnow,
    )
    assert result["residual_result"] == "PASS"
    assert result["final_authoritative_open_orders"] == 0
