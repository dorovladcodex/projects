from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any

from app.bybit.demo_diagnostics import (
    DemoDiagnosticsConfig,
    DemoDiagnosticsError,
    ReadOnlyBybitDemoClient,
)
from app.db.persistence import PersistenceRepository
from app.models import DemoExecutionRecord, DemoExecutionState


TERMINAL_ORDER_STATES = {"Filled", "Cancelled", "Rejected", "Deactivated"}


@dataclass
class DemoExecutionDiagnosis:
    record: DemoExecutionRecord
    durable_events: list[dict[str, Any]]
    entry_order_history: list[dict[str, Any]]
    close_order_history: list[dict[str, Any]]
    entry_executions: list[dict[str, Any]]
    close_executions: list[dict[str, Any]]
    closed_pnl: list[dict[str, Any]]
    remote_positions: list[dict[str, Any]]
    remote_open_orders: list[dict[str, Any]]
    bot_owned_open_orders: list[dict[str, Any]]
    unrelated_open_orders: list[dict[str, Any]]
    conclusion: str
    proposed_state: DemoExecutionState | None
    blockers: list[str]
    gross_realized_pnl: Decimal
    net_realized_pnl: Decimal
    entry_fees: Decimal
    close_fees: Decimal

    @property
    def repairable(self) -> bool:
        return self.proposed_state is not None and not self.blockers


def diagnose_demo_execution(
    config: DemoDiagnosticsConfig,
    execution_id: str,
    *,
    repository: PersistenceRepository | None = None,
    client: ReadOnlyBybitDemoClient | None = None,
) -> DemoExecutionDiagnosis:
    if config.rest_url != "https://api-demo.bybit.com":
        raise DemoDiagnosticsError("exact Demo REST domain is required")
    repo = repository or PersistenceRepository(config.database_url, create_schema=False)
    if not repo.available:
        raise DemoDiagnosticsError("database persistence is unavailable")
    record = next(
        (item for item in repo.load_demo_executions() if str(item.id) == execution_id),
        None,
    )
    if record is None:
        raise DemoDiagnosticsError("durable Demo execution was not found")
    read_client = client or ReadOnlyBybitDemoClient(
        config.api_key, config.api_secret, base_url=config.rest_url
    )
    read_client.verify()
    history = read_client.get_order_history(record.symbol)
    executions = read_client.get_executions(record.symbol)
    positions = read_client.get_positions(record.symbol)
    open_orders = read_client.get_open_orders()
    closed_pnl = read_client.get_closed_pnl(record.symbol)

    entry_history = [
        item for item in history
        if _matches(item, record.order_id, record.order_link_id)
    ]
    close_history = [
        item for item in history
        if _matches(item, record.close_order_id, record.close_order_link_id)
    ]
    entry_fills = [
        item for item in executions
        if _matches(item, record.order_id, record.order_link_id)
    ]
    close_fills = [
        item for item in executions
        if _matches(item, record.close_order_id, record.close_order_link_id)
    ]
    known_links = {
        link for link in (record.order_link_id, record.close_order_link_id) if link
    }
    bot_orders = [
        item for item in open_orders
        if str(item.get("orderLinkId") or "") in known_links
    ]
    unrelated = [item for item in open_orders if item not in bot_orders]
    active_positions = [item for item in positions if _decimal(item.get("size")) > 0]

    blockers: list[str] = []
    if active_positions:
        blockers.append("remote position is not flat")
    if bot_orders:
        blockers.append("bot-owned open order exists")
    if record.order_id and not entry_history and not entry_fills:
        blockers.append("entry order is absent from authoritative history")
    if record.close_order_id and not close_history and not close_fills:
        blockers.append("close order is absent from authoritative history")
    for label, rows in (("entry", entry_history), ("close", close_history)):
        if any(str(item.get("orderStatus") or "") not in TERMINAL_ORDER_STATES for item in rows):
            blockers.append(f"{label} order is not terminal")

    entry_qty = sum((_decimal(item.get("execQty")) for item in entry_fills), Decimal("0"))
    close_qty = sum((_decimal(item.get("execQty")) for item in close_fills), Decimal("0"))
    entry_fees = sum((_decimal(item.get("execFee")) for item in entry_fills), Decimal("0"))
    close_fees = sum((_decimal(item.get("execFee")) for item in close_fills), Decimal("0"))
    entry_average = _weighted_average(entry_fills)
    close_average = _weighted_average(close_fills)
    direction = Decimal("1") if record.side.value == "BUY" else Decimal("-1")
    gross = (
        (close_average - entry_average) * min(entry_qty, close_qty) * direction
        if entry_average > 0 and close_average > 0 else Decimal("0")
    )
    net = gross - entry_fees - close_fees
    matching_closed_pnl = [
        item for item in closed_pnl
        if record.close_order_id
        and str(item.get("orderId") or "") == record.close_order_id
    ]
    if matching_closed_pnl:
        net = _decimal(matching_closed_pnl[0].get("closedPnl"))

    proposed: DemoExecutionState | None = None
    if not record.order_id and not entry_history and not entry_fills:
        conclusion = "submitted no exchange order"
        proposed = DemoExecutionState.DEMO_NOT_SUBMITTED
    elif entry_qty == 0 and entry_history and all(
        str(item.get("orderStatus") or "") in {"Cancelled", "Rejected", "Deactivated"}
        for item in entry_history
    ):
        conclusion = "submitted but did not fill"
        proposed = DemoExecutionState.DEMO_ORDER_CANCELLED
    elif Decimal("0") < entry_qty < record.requested_quantity:
        conclusion = "partially filled"
        blockers.append("partial fill requires manual reconciliation")
    elif entry_qty >= record.requested_quantity and close_qty >= entry_qty and not active_positions:
        conclusion = "fully filled and later closed"
        proposed = DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION
    elif entry_qty >= record.requested_quantity:
        conclusion = "fully filled with unresolved close state"
        blockers.append("filled entry is not conclusively closed")
    else:
        conclusion = "failed flat state verified without conclusive fill"
        proposed = DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED

    return DemoExecutionDiagnosis(
        record=record,
        durable_events=repo.load_demo_execution_events(execution_id),
        entry_order_history=entry_history,
        close_order_history=close_history,
        entry_executions=entry_fills,
        close_executions=close_fills,
        closed_pnl=matching_closed_pnl,
        remote_positions=positions,
        remote_open_orders=open_orders,
        bot_owned_open_orders=bot_orders,
        unrelated_open_orders=unrelated,
        conclusion=conclusion,
        proposed_state=proposed,
        blockers=list(dict.fromkeys(blockers)),
        gross_realized_pnl=gross,
        net_realized_pnl=net,
        entry_fees=entry_fees,
        close_fees=close_fees,
    )


def apply_demo_execution_repair(
    diagnosis: DemoExecutionDiagnosis,
    repository: PersistenceRepository,
) -> bool:
    if not diagnosis.repairable or diagnosis.proposed_state is None:
        return False
    if (
        diagnosis.record.state == diagnosis.proposed_state
        and diagnosis.record.cleanup_result
    ):
        return True
    record = diagnosis.record.model_copy(deep=True)
    record.state = diagnosis.proposed_state
    record.failure_reason = "Windows sleep/resume interrupted canary client workflow"
    record.cleanup_result = "remote position flat and bot-owned orders zero"
    record.realized_exchange_pnl = diagnosis.net_realized_pnl
    record.exchange_fees = diagnosis.entry_fees + diagnosis.close_fees
    if diagnosis.close_executions:
        record.average_close_price = _weighted_average(diagnosis.close_executions)
    record.last_error = None
    record.last_reconciliation_at = datetime.now(timezone.utc)
    record.updated_at = record.last_reconciliation_at
    repair_payload = {
        "repair_timestamp": record.updated_at.isoformat(),
        "repair_reason": record.failure_reason,
        "entry_order_states": [
            str(item.get("orderStatus") or "")
            for item in diagnosis.entry_order_history
        ],
        "close_order_states": [
            str(item.get("orderStatus") or "")
            for item in diagnosis.close_order_history
        ],
        "final_position": _json_safe(diagnosis.remote_positions),
        "final_open_order_count": len(diagnosis.remote_open_orders),
    }
    return repository.repair_demo_execution(
        record,
        event_types=[
            "SLEEP_RESUME_INTERRUPTION_DETECTED",
            "READ_ONLY_RECONCILIATION_COMPLETED",
            "EXECUTION_REPAIR_APPLIED",
            "FINAL_REMOTE_STATE_FLAT",
        ],
        repair_payload=repair_payload,
    )


def diagnosis_payload(diagnosis: DemoExecutionDiagnosis) -> dict[str, Any]:
    record = diagnosis.record
    return {
        "execution_id": str(record.id),
        "candidate_id": str(record.candidate_id),
        "risk_decision_id": record.risk_decision_id,
        "run_id": record.run_id,
        "order_link_id": record.order_link_id,
        "entry_order_id": record.order_id,
        "close_order_id": record.close_order_id,
        "durable_state": record.state.value,
        "durable_transitions": diagnosis.durable_events,
        "quantity_requested": str(record.requested_quantity),
        "quantity_executed": str(sum((_decimal(x.get("execQty")) for x in diagnosis.entry_executions), Decimal("0"))),
        "entry_average_price": str(_weighted_average(diagnosis.entry_executions)),
        "close_average_price": str(_weighted_average(diagnosis.close_executions)),
        "entry_fees": str(diagnosis.entry_fees),
        "close_fees": str(diagnosis.close_fees),
        "gross_realized_pnl": str(diagnosis.gross_realized_pnl),
        "net_realized_pnl": str(diagnosis.net_realized_pnl),
        "tp_sl_attempts": [event for event in diagnosis.durable_events if event["event_type"] in {"PROTECTION_PENDING", "DEMO_POSITION_OPEN"}],
        "cleanup_attempts": [event for event in diagnosis.durable_events if event["event_type"] in {"CLOSE_SUBMITTING", "CLOSE_ACK", "CLOSE_Filled"}],
        "entry_order_history": _json_safe(diagnosis.entry_order_history),
        "close_order_history": _json_safe(diagnosis.close_order_history),
        "entry_executions": _json_safe(diagnosis.entry_executions),
        "close_executions": _json_safe(diagnosis.close_executions),
        "final_remote_position": _json_safe(diagnosis.remote_positions),
        "final_remote_open_orders": _json_safe(diagnosis.remote_open_orders),
        "unrelated_open_orders": _json_safe(diagnosis.unrelated_open_orders),
        "conclusion": diagnosis.conclusion,
        "proposed_terminal_state": diagnosis.proposed_state.value if diagnosis.proposed_state else None,
        "repairable": diagnosis.repairable,
        "blockers": diagnosis.blockers,
    }


def _matches(item: dict[str, Any], order_id: str | None, link_id: str | None) -> bool:
    return bool(
        (order_id and str(item.get("orderId") or "") == order_id)
        or (link_id and str(item.get("orderLinkId") or "") == link_id)
    )


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception as exc:
        raise DemoDiagnosticsError("invalid exchange decimal") from exc


def _weighted_average(rows: list[dict[str, Any]]) -> Decimal:
    quantity = sum((_decimal(item.get("execQty")) for item in rows), Decimal("0"))
    if quantity <= 0:
        return Decimal("0")
    value = sum(
        (_decimal(item.get("execQty")) * _decimal(item.get("execPrice")) for item in rows),
        Decimal("0"),
    )
    return value / quantity


def _json_safe(value: object) -> object:
    return json.loads(json.dumps(value, default=str))
