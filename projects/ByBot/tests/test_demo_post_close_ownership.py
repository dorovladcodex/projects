from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from uuid import UUID, uuid4

from app.bybit.demo import (
    DemoExecutionService,
    classify_demo_order_ownership,
)
from app.models import (
    DemoExecutionRecord,
    DemoExecutionState,
    ExecutionEnvironment,
    Side,
    Symbol,
)
from tests.test_bybit_demo_execution import (
    FakeDemoClient,
    MemoryRepository,
    demo_settings,
)


FIXTURE = Path(__file__).parent / "fixtures" / "demo_replay" / (
    "avax_post_close_residual_20260725.json"
)


def _incident() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _record(
    *,
    state: DemoExecutionState = DemoExecutionState.DEMO_CLOSED,
    closed_at: datetime | None = None,
) -> DemoExecutionRecord:
    data = _incident()
    return DemoExecutionRecord(
        id=UUID(data["execution_id"]),
        candidate_id=UUID(data["candidate_id"]),
        run_id=data["run_id"],
        execution_environment=ExecutionEnvironment.BYBIT_DEMO,
        order_link_id="bybot-v2-avax-entry",
        state=state,
        symbol=Symbol.AVAXUSDT,
        side=Side.SELL,
        requested_quantity=Decimal(data["accepted_quantity"]),
        accepted_quantity=Decimal(data["accepted_quantity"]),
        order_id=data["entry_order_id"],
        close_order_id=data["close_order_id"],
        close_order_link_id="bybot-v2-avax-close",
        tp_order_id=data["protection_orders"][0]["orderId"],
        sl_order_id=data["protection_orders"][1]["orderId"],
        take_profit=Decimal("6.606"),
        stop_loss=Decimal("6.673"),
        closed_at=closed_at or datetime.now(timezone.utc),
    )


def _flat_positions() -> list[dict]:
    return [
        {"symbol": "AVAXUSDT", "size": "0", "positionIdx": 0},
    ]


class AutoCancelClient(FakeDemoClient):
    def __init__(self, orders: list[dict], *, remove_on_cancel: bool = True):
        super().__init__()
        self.open_orders = [dict(item) for item in orders]
        self.positions = _flat_positions()
        self.remove_on_cancel = remove_on_cancel

    def cancel_order(self, symbol, order_id):
        super().cancel_order(symbol, order_id)
        if self.remove_on_cancel:
            self.open_orders = [
                item
                for item in self.open_orders
                if str(item.get("orderId") or "") != order_id
            ]
        return {"retCode": 0}


def test_avax_terminal_tp_sl_are_owned_pending_cancel_not_unrelated() -> None:
    data = _incident()
    record = _record()
    result = classify_demo_order_ownership(
        data["protection_orders"],
        [record],
        _flat_positions(),
        now=record.closed_at + timedelta(seconds=2),
        terminal_residual_timeout_seconds=5,
    )

    assert {item["orderId"] for item in result["bot_owned_pending_cancel"]} == {
        record.tp_order_id,
        record.sl_order_id,
    }
    assert result["unrelated_external"] == []
    assert result["ownership_conflicts"] == []


def test_terminal_order_ownership_survives_restart_and_exact_cancel_is_idempotent() -> None:
    data = _incident()
    repository = MemoryRepository()
    record = _record()
    repository.records[str(record.candidate_id)] = record
    client = AutoCancelClient(data["protection_orders"])
    settings = demo_settings(
        demo_terminal_residual_cancel_timeout_seconds=1,
    )

    first = DemoExecutionService(settings, repository, client, run_id=record.run_id)
    result = first.reconcile()
    second = DemoExecutionService(settings, repository, client, run_id=record.run_id)
    second_result = second.reconcile()

    assert result["confirmed_unrelated_orders"] == 0
    assert result["ownership_conflicts"] == 0
    assert result["remote_orders"] == 0
    assert {item[1] for item in client.cancelled} == {
        record.tp_order_id,
        record.sl_order_id,
    }
    assert len(client.cancelled) == 2
    assert second_result["remote_orders"] == 0
    assert len(client.cancelled) == 2
    restored = repository.load_demo_executions()[0]
    assert restored.state == DemoExecutionState.DEMO_CLOSED
    assert restored.tp_order_id == record.tp_order_id
    assert restored.sl_order_id == record.sl_order_id
    assert restored.cleanup_result == (
        "remote position flat and bot-owned orders zero"
    )
    assert not any("EMERGENCY" in event for event, _state in repository.saved_events)


def test_authoritative_reconcile_replaces_stale_cached_unrelated_snapshot() -> None:
    repository = MemoryRepository()
    record = _record()
    repository.records[str(record.candidate_id)] = record
    client = AutoCancelClient([])
    service = DemoExecutionService(
        demo_settings(demo_terminal_residual_cancel_timeout_seconds=1),
        repository,
        client,
        run_id=record.run_id,
    )
    service.confirmed_unrelated_orders = 2
    service.unrelated_open_orders = 2

    assert service.as_status()["confirmed_unrelated_orders"] == 2
    service.reconcile()
    status = service.as_status()
    assert status["confirmed_unrelated_orders"] == 0
    assert status["unrelated_open_orders"] == 0
    assert status["order_snapshot_age_ms"] is not None


def test_unrelated_manual_order_is_not_attributed_by_symbol() -> None:
    record = _record()
    manual = {
        "orderId": "manual-order",
        "orderLinkId": "",
        "symbol": "AVAXUSDT",
        "side": "Buy",
        "reduceOnly": True,
        "closeOnTrigger": True,
        "stopOrderType": "TakeProfit",
        "qty": "15.1",
        "triggerPrice": "6.606",
    }

    result = classify_demo_order_ownership(
        [manual], [record], _flat_positions()
    )

    assert result["unrelated_external"] == [manual]
    assert result["bot_owned_pending_cancel"] == []


def test_reused_exact_order_id_is_an_ownership_conflict() -> None:
    data = _incident()
    first = _record()
    second = _record()
    second.id = uuid4()
    second.candidate_id = uuid4()
    second.run_id = "other-run"
    second.tp_order_id = first.tp_order_id

    result = classify_demo_order_ownership(
        [data["protection_orders"][0]],
        [first, second],
        _flat_positions(),
    )

    assert result["ownership_conflicts"] == [data["protection_orders"][0]]
    assert result["bot_owned_pending_cancel"] == []


def test_terminal_residual_timeout_is_precise_and_does_not_repeat_cancel() -> None:
    data = _incident()
    repository = MemoryRepository()
    record = _record(closed_at=datetime.now(timezone.utc) - timedelta(seconds=10))
    repository.records[str(record.candidate_id)] = record
    client = AutoCancelClient(data["protection_orders"], remove_on_cancel=False)
    service = DemoExecutionService(
        demo_settings(demo_terminal_residual_cancel_timeout_seconds=1),
        repository,
        client,
        run_id=record.run_id,
    )

    result = service.reconcile()

    assert result["bot_owned_terminal_residual_orders"] == 2
    assert result["confirmed_unrelated_orders"] == 0
    assert service.kill_switch_active is True
    assert "terminal protection residual cancellation timeout" in (
        service.kill_switch_reasons[-1]
    )
    assert len(client.cancelled) == 2
