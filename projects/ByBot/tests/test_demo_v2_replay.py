from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.bybit.demo_replay import DemoReplayFixture, DemoV2ReplayHarness
from app.models import (
    DemoExecutionRecord, DemoExecutionState, DemoFill, Side, Symbol,
)
from app.v2.analytics import V2ReportGenerator


FIXTURES = Path(__file__).parent / "fixtures" / "demo_replay"


@pytest.mark.parametrize(
    "name,attribution",
    [
        ("ada_take_profit.json", "take_profit"),
        ("link_stop_loss.json", "stop_loss"),
        ("wif_stop_loss.json", "stop_loss"),
        ("eth_stop_loss.json", "stop_loss"),
    ],
)
def test_incident_replay_terminalizes_once_without_exchange_mutation(
    name: str, attribution: str,
) -> None:
    fixture = DemoReplayFixture.load(FIXTURES / name)
    result = DemoV2ReplayHarness(fixture).run()

    assert result.record.state == DemoExecutionState.DEMO_CLOSED
    assert result.record.exit_attribution == attribution
    assert result.completed_trades == 1
    assert result.open_count == 0
    assert result.reservation_release_count == 1
    assert result.cooldown_update_count == 1
    assert result.unresolved_execution_ids == []
    assert result.terminal_event_count == 1
    assert result.exchange_mutation_attempts == 0


def test_sparse_lifecycle_event_cannot_erase_exact_entry_identity() -> None:
    fixture = DemoReplayFixture.load(FIXTURES / "ada_take_profit.json")
    assert fixture.sparse_entry_fill_order_id is True

    result = DemoV2ReplayHarness(fixture).run()

    assert result.record.order_id == fixture.entry_order_id
    assert result.record.order_link_id == fixture.order_link_id


def _older_profitable_near(*, run_id: str) -> DemoExecutionRecord:
    opened = datetime(2026, 7, 20, 15, 27, tzinfo=timezone.utc)
    closed = opened + timedelta(minutes=5)
    return DemoExecutionRecord(
        id=UUID("e778e1f8-1335-4a47-9797-608a62b3ce41"),
        candidate_id=uuid4(), run_id=run_id,
        order_link_id="bybot-older-near-entry",
        order_id="older-near-entry-order",
        close_order_id="older-near-take-profit-order",
        state=DemoExecutionState.DEMO_CLOSED,
        symbol=Symbol.NEARUSDT, side=Side.BUY,
        strategy_name="OIFundingSqueezeStrategy",
        strategy_version="2.1.0",
        requested_quantity=Decimal("25"),
        accepted_quantity=Decimal("25"),
        average_fill_price=Decimal("1.86"),
        average_close_price=Decimal("1.868"),
        fills=[DemoFill(
            execution_id="older-near-entry-exec",
            order_id="older-near-entry-order", quantity=Decimal("25"),
            price=Decimal("1.86"), fee=Decimal("0.027"),
            fee_currency="USDT", executed_at=opened,
        )],
        close_fills=[DemoFill(
            execution_id="older-near-close-exec",
            order_id="older-near-take-profit-order", quantity=Decimal("25"),
            price=Decimal("1.868"), fee=Decimal("0.02884303"),
            fee_currency="USDT", executed_at=closed,
        )],
        exchange_fees=Decimal("0.05584303"),
        gross_realized_pnl=Decimal("0.2"),
        realized_exchange_pnl=Decimal("0.14415697"),
        paper_shadow_pnl=Decimal("0.14415697"),
        close_reason="take_profit", exit_attribution="take_profit",
        closed_at=closed, created_at=opened, updated_at=closed,
    )


@pytest.mark.parametrize(
    "older_run_id,stale_rows",
    [
        ("demo-v2-20260720T152726944Z", [
            {"symbol": "NEARUSDT", "orderId": "older-near-take-profit-order",
             "closedPnl": "0.14415697"},
        ]),
        ("demo-v2-20260722T082205376Z", [
            {"symbol": "NEARUSDT", "orderId": "older-near-take-profit-order",
             "closedPnl": "0.14415697"},
        ]),
        ("demo-v2-20260720T152726944Z", [
            {"symbol": "NEARUSDT", "orderId": "unrelated-newer-order",
             "closedPnl": "9.99"},
            {"symbol": "NEARUSDT", "orderId": "older-near-take-profit-order",
             "closedPnl": "0.14415697"},
        ]),
    ],
)
def test_near_stop_loss_replay_never_uses_symbol_only_closed_pnl(
    older_run_id: str, stale_rows: list[dict[str, str]],
) -> None:
    fixture = DemoReplayFixture.load(FIXTURES / "near_stop_loss_20260722.json")
    older = _older_profitable_near(run_id=older_run_id)

    result = DemoV2ReplayHarness(
        fixture, historical_records=[older], stale_closed_pnl=stale_rows,
    ).run()

    assert result.record.state == DemoExecutionState.DEMO_CLOSED
    assert result.record.run_id == fixture.run_id
    assert result.record.close_order_id == fixture.close_order_id
    assert [fill.execution_id for fill in result.record.close_fills] == [
        fixture.close_execution_id
    ]
    assert result.record.exit_attribution == "stop_loss"
    assert result.record.average_close_price == Decimal("1.8725")
    assert result.record.gross_realized_pnl == Decimal("-0.12549")
    assert result.record.exchange_fees == Decimal("0.05492632")
    assert result.record.realized_exchange_pnl == Decimal("-0.18041632")
    assert result.completed_trades == 1
    assert result.terminal_event_count == 1
    assert result.risk_ledger_application_count == 1
    assert result.exchange_mutation_attempts == 0


def test_duplicate_close_and_restart_during_closing_remain_idempotent() -> None:
    fixture = DemoReplayFixture.load(FIXTURES / "near_stop_loss_20260722.json")
    result = DemoV2ReplayHarness(
        fixture, historical_records=[
            _older_profitable_near(run_id="demo-v2-20260720T152726944Z")
        ],
        stale_closed_pnl=[{
            "symbol": "NEARUSDT",
            "orderId": "older-near-take-profit-order",
            "closedPnl": "0.14415697",
        }],
        duplicate_close_event=True,
        restart_during_closing=True,
    ).run()

    assert result.record.realized_exchange_pnl == Decimal("-0.18041632")
    assert len(result.record.close_fills) == 1
    assert result.terminal_event_count == 1
    assert result.risk_ledger_application_count == 1
    assert result.unresolved_execution_ids == []


@pytest.mark.parametrize(
    "name,expected_pnl",
    [
        ("eth_protection_pending_20260724.json", Decimal("0.34716065")),
        ("xrp_late_stop_20260724.json", Decimal("-0.37705234")),
    ],
)
@pytest.mark.parametrize(
    "miss_close_websocket,restart_before_close",
    [(False, False), (True, False), (False, True)],
)
def test_20260724_incidents_terminalize_from_exact_rest_evidence(
    name: str,
    expected_pnl: Decimal,
    miss_close_websocket: bool,
    restart_before_close: bool,
) -> None:
    fixture = DemoReplayFixture.load(FIXTURES / name)
    result = DemoV2ReplayHarness(
        fixture,
        miss_close_websocket=miss_close_websocket,
        restart_before_close=restart_before_close,
        duplicate_close_event=not miss_close_websocket,
    ).run()

    assert result.record.state == DemoExecutionState.DEMO_CLOSED
    assert result.record.realized_exchange_pnl == expected_pnl
    assert result.terminal_event_count == 1
    assert result.risk_ledger_application_count == 1
    assert result.reservation_release_count == 1
    assert result.unresolved_execution_ids == []
    assert result.exchange_mutation_attempts == 0


@pytest.mark.parametrize(
    "state",
    [
        DemoExecutionState.DEMO_PROTECTION_PENDING,
        DemoExecutionState.DEMO_POSITION_OPEN,
        DemoExecutionState.DEMO_CLOSING,
        DemoExecutionState.DEMO_RECONCILIATION_REQUIRED,
    ],
)
def test_exact_full_close_terminalizes_from_every_owned_nonterminal_state(
    state: DemoExecutionState,
) -> None:
    fixture = replace(
        DemoReplayFixture.load(
            FIXTURES / "eth_protection_pending_20260724.json"
        ),
        initial_close_state=state,
    )

    result = DemoV2ReplayHarness(fixture, miss_close_websocket=True).run()

    assert result.record.state == DemoExecutionState.DEMO_CLOSED
    assert result.terminal_event_count == 1


def test_reporting_and_durable_ledger_use_same_exact_near_execution(
    tmp_path: Path,
) -> None:
    fixture = DemoReplayFixture.load(FIXTURES / "near_stop_loss_20260722.json")
    older = _older_profitable_near(run_id="demo-v2-20260720T152726944Z")
    result = DemoV2ReplayHarness(
        fixture,
        historical_records=[older],
        stale_closed_pnl=[{
            "symbol": "NEARUSDT",
            "orderId": older.close_order_id,
            "closedPnl": str(older.realized_exchange_pnl),
        }],
    ).run()
    current = result.record.model_dump(mode="json")
    historical = older.model_dump(mode="json")

    class RunScopedRepository:
        def v2_report_rows(self, run_id: str):
            executions = [current, historical]
            return {
                "signals": [], "rejections": [], "incidents": [], "runtime": {},
                "executions": [row for row in executions if row["run_id"] == run_id],
            }

    report = V2ReportGenerator(
        RunScopedRepository(), str(tmp_path)
    ).generate(fixture.run_id)

    assert report["completed_trades"] == 1
    assert Decimal(report["net_pnl"]) == Decimal("-0.18041632")
    assert report["unattributed_exit_count"] == 0
    assert report["exit_counts"] == {"stop_loss": 1}
    assert result.risk_ledger_application_count == 1
    csv_text = (tmp_path / fixture.run_id / "trades.csv").read_text(
        encoding="utf-8"
    )
    assert fixture.close_order_id in csv_text
    assert "-0.18041632" in csv_text
    assert "0.14415697" not in csv_text
