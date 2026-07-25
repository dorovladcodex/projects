from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.bybit.demo import _capture_protection_order_ownership
from app.bybit.demo_diagnostics import (
    DemoDiagnosticsConfig,
    classify_demo_open_orders,
    run_demo_diagnostics,
)
from app.models import DemoExecutionRecord, DemoExecutionState, Side, Symbol
from app.v2.drain import V2DrainController, V2RunPhase
from tests.test_v2_runtime_observability import runtime


def execution(
    symbol: Symbol = Symbol.ETHUSDT,
    *,
    execution_id: object | None = None,
    quantity: str = "0.04",
) -> DemoExecutionRecord:
    return DemoExecutionRecord(
        id=execution_id or uuid4(),
        candidate_id=uuid4(),
        run_id="drain-run",
        order_link_id=f"bybot-test-{symbol.value.lower()}",
        order_id=f"entry-{symbol.value.lower()}",
        state=DemoExecutionState.DEMO_POSITION_OPEN,
        symbol=symbol,
        side=Side.BUY,
        requested_quantity=Decimal(quantity),
        accepted_quantity=Decimal(quantity),
        average_fill_price=Decimal("100"),
        take_profit=Decimal("110"),
        stop_loss=Decimal("90"),
        protection_confirmed=True,
    )


def protection_order(
    item: DemoExecutionRecord, stop_type: str, order_id: str
) -> dict[str, object]:
    take_profit = stop_type == "TakeProfit"
    return {
        "orderId": order_id,
        "orderLinkId": "",
        "symbol": item.symbol.value,
        "positionIdx": 0,
        "side": "Sell",
        "reduceOnly": True,
        "closeOnTrigger": True,
        "stopOrderType": stop_type,
        "createType": "CreateByTakeProfit" if take_profit else "CreateByStopLoss",
        "triggerDirection": 1 if take_profit else 2,
        "qty": str(item.accepted_quantity),
        "triggerPrice": str(item.take_profit if take_profit else item.stop_loss),
    }


def position(item: DemoExecutionRecord) -> dict[str, str | int]:
    return {
        "symbol": item.symbol.value,
        "size": str(item.accepted_quantity),
        "side": "Buy",
        "positionIdx": 0,
        "takeProfit": str(item.take_profit),
        "stopLoss": str(item.stop_loss),
    }


def test_entry_window_immediately_before_nominal_end_is_draining() -> None:
    nominal = datetime(2026, 7, 20, 14, 25, tzinfo=timezone.utc)
    drain = V2DrainController(nominal, lead_seconds=300, timeout_seconds=900)

    status = drain.evaluate(
        now=nominal - timedelta(seconds=80), active_execution_ids=[]
    )

    assert status.phase == V2RunPhase.DRAINING
    assert status.entries_allowed is False


def test_drain_phase_blocks_new_entries(tmp_path: Path) -> None:
    app, repo, exchange = runtime(tmp_path, (Symbol.BTCUSDT,))
    app.drain = V2DrainController(
        datetime.now(timezone.utc) + timedelta(seconds=60),
        lead_seconds=300,
        timeout_seconds=900,
    )

    asyncio.run(app.cycle())

    assert app.stop_new_entries is True
    assert repo.signals == []
    assert exchange.calls == 0


def test_active_position_monitoring_continues_during_drain(tmp_path: Path) -> None:
    app, repo, exchange = runtime(tmp_path, (Symbol.ETHUSDT,))
    item = execution()
    item.run_id = app.run_id
    repo.load_demo_executions = lambda: [item]
    observed: list[str] = []
    exchange.demo_execution.monitor_strategy_position = (
        lambda execution_id, *_args, **_kwargs: observed.append(execution_id)
    )
    app.drain = V2DrainController(
        datetime.now(timezone.utc) + timedelta(seconds=30),
        lead_seconds=300,
        timeout_seconds=900,
    )

    asyncio.run(app.cycle())

    assert observed == [str(item.id)]
    assert app.stop_new_entries is True


def test_terminal_close_reconciliation_finishes_drain(tmp_path: Path) -> None:
    app, repo, exchange = runtime(tmp_path, (Symbol.ETHUSDT,))
    item = execution()
    item.run_id = app.run_id
    repo.load_demo_executions = lambda: [item]

    def close_during_monitor(*_args, **_kwargs) -> None:
        item.state = DemoExecutionState.DEMO_CLOSED
        item.closed_at = datetime.now(timezone.utc)

    exchange.demo_execution.monitor_strategy_position = close_during_monitor
    app.drain = V2DrainController(
        datetime.now(timezone.utc) - timedelta(seconds=1),
        lead_seconds=300,
        timeout_seconds=900,
    )

    asyncio.run(app.cycle())

    assert app.status()["run_phase"] == "FINISHED"
    assert app.status()["drain_active_execution_ids"] == []


def test_bounded_drain_timeout_keeps_active_execution_as_blocker() -> None:
    nominal = datetime(2026, 7, 20, 14, 25, tzinfo=timezone.utc)
    drain = V2DrainController(nominal, lead_seconds=300, timeout_seconds=60)

    status = drain.evaluate(
        now=nominal + timedelta(seconds=61), active_execution_ids=["execution-1"]
    )

    assert status.phase == V2RunPhase.RECONCILING
    assert status.timed_out is True
    assert status.active_execution_ids == ("execution-1",)


def test_three_concurrent_positions_drain_independently() -> None:
    nominal = datetime(2026, 7, 20, 14, 25, tzinfo=timezone.utc)
    drain = V2DrainController(nominal, lead_seconds=300, timeout_seconds=900)
    active = ["near", "eth", "sol"]

    status = drain.evaluate(now=nominal, active_execution_ids=active)
    assert status.phase == V2RunPhase.RECONCILING
    assert status.active_execution_ids == ("eth", "near", "sol")

    status = drain.evaluate(now=nominal + timedelta(seconds=1), active_execution_ids=[])
    assert status.phase == V2RunPhase.FINISHED


def test_operator_draining_without_nominal_deadline_finishes_after_late_close() -> None:
    drain = V2DrainController(None, lead_seconds=300, timeout_seconds=900)

    drain.force_draining()
    waiting = drain.evaluate(active_execution_ids=["late-close"])
    finished = drain.evaluate(active_execution_ids=[])

    assert waiting.phase == V2RunPhase.DRAINING
    assert finished.phase == V2RunPhase.FINISHED


def test_exchange_generated_tp_sl_are_owned_and_ids_are_persisted() -> None:
    item = execution()
    tp = protection_order(item, "TakeProfit", "tp-id")
    sl = protection_order(item, "StopLoss", "sl-id")
    positions = {item.symbol.value: {
        "size": str(item.accepted_quantity), "side": "Buy", "position_idx": "0",
        "take_profit": str(item.take_profit), "stop_loss": str(item.stop_loss),
    }}

    classified = classify_demo_open_orders([tp, sl], [item], positions)
    assert classified["take_profit"] == [tp]
    assert classified["stop_loss"] == [sl]
    assert classified["unrelated"] == []
    assert _capture_protection_order_ownership(item, [tp, sl], [position(item)])
    assert item.tp_order_id == "tp-id"
    assert item.sl_order_id == "sl-id"


def test_manual_order_with_same_symbol_remains_unrelated() -> None:
    item = execution()
    manual = {
        **protection_order(item, "TakeProfit", "manual"),
        "reduceOnly": False,
        "createType": "CreateByUser",
    }
    positions = {item.symbol.value: {
        "size": str(item.accepted_quantity), "side": "Buy", "position_idx": "0",
        "take_profit": str(item.take_profit), "stop_loss": str(item.stop_loss),
    }}

    result = classify_demo_open_orders([manual], [item], positions)

    assert result["unrelated"] == [manual]
    assert result["take_profit"] == []


class DiagnosticRepository:
    available = True

    def __init__(self, items: list[DemoExecutionRecord]) -> None:
        self.items = items

    def load_demo_kill_switch(self):
        return {"active": False, "reasons": [], "events": []}

    def load_demo_executions(self):
        return list(self.items)

    def load_demo_execution_events(self, _execution_id):
        return []


class AllSymbolReadClient:
    def __init__(self, item: DemoExecutionRecord | None = None) -> None:
        self.item = item
        self.position_queries: list[str] = []

    def verify(self):
        return None

    def get_open_orders(self):
        return []

    def get_positions(self, symbol):
        self.position_queries.append(symbol.value)
        return [position(self.item)] if self.item and symbol == self.item.symbol else []

    def get_order_history(self, _symbol):
        return []


def diagnostic_config(symbols: tuple[str, ...]) -> DemoDiagnosticsConfig:
    return DemoDiagnosticsConfig(
        "sqlite://", "fake-key", "fake-secret", universe_symbols=symbols
    )


def test_diagnostics_checks_every_configured_symbol() -> None:
    client = AllSymbolReadClient()
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

    result = run_demo_diagnostics(
        diagnostic_config(symbols),
        repository=DiagnosticRepository([]),
        client=client,
    )

    assert result.passed
    assert set(client.position_queries) == set(symbols)
    assert set(result.positions) == set(symbols)


def test_remaining_owned_position_produces_safety_failure_without_mutation() -> None:
    item = execution()
    client = AllSymbolReadClient(item)

    result = run_demo_diagnostics(
        diagnostic_config((item.symbol.value,)),
        repository=DiagnosticRepository([item]),
        client=client,
    )

    assert result.passed is False
    assert str(item.id) in [str(row.id) for row in result.unresolved_executions]
    assert result.position_ownership[item.symbol.value] == [str(item.id)]
    for method in ("create_order", "cancel_order", "close_position"):
        assert not hasattr(client, method)


def test_runner_waits_for_drain_and_never_auto_cleans_account() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts" / "demo_v2_soak.ps1").read_text(
        encoding="utf-8"
    )
    assert "run_phase -eq 'FINISHED'" in source
    assert "$status.drain_timed_out" in source
    assert "drain_active_execution_ids" in source
    cleanup_index = source.index('if ($OptionalForceDemoCleanup)')
    assert cleanup_index > source.index("while (-not $drainComplete")
