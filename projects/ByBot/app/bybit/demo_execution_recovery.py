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
from app.bybit.demo import classify_exchange_close
from app.bybit.demo_accounting import calculate_fill_level_accounting
from app.db.persistence import PersistenceRepository
from app.models import (
    DemoExecutionRecord,
    DemoExecutionState,
    DemoFill,
    ExecutionEnvironment,
)


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
    transaction_log: list[dict[str, Any]]
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
    close_source: str | None
    repeated_remote_flat: bool
    rejected_close_order_ids: list[str]
    rejected_close_execution_ids: list[str]
    conflicting_order_ids: list[str]
    conflicting_execution_ids: list[str]
    funding_pnl: Decimal
    funding_transaction_ids: list[str]
    accounting_components: dict[str, Any]
    accounting_status: str

    @property
    def repairable(self) -> bool:
        return self.proposed_state is not None and not self.blockers


def exact_close_reconciliation_blockers(
    diagnosis: DemoExecutionDiagnosis,
) -> list[str]:
    """Return fail-closed blockers for an exact, authoritative full close."""
    record = diagnosis.record
    blockers = list(diagnosis.blockers)
    entry_quantity = sum(
        (_decimal(item.get("execQty")) for item in diagnosis.entry_executions),
        Decimal("0"),
    )
    close_quantity = sum(
        (_decimal(item.get("execQty")) for item in diagnosis.close_executions),
        Decimal("0"),
    )
    active_symbol_position = any(
        str(item.get("symbol") or "") == record.symbol.value
        and _decimal(item.get("size")) > 0
        for item in diagnosis.remote_positions
    )
    if record.execution_environment != ExecutionEnvironment.BYBIT_DEMO:
        blockers.append("execution does not belong to BYBIT_DEMO")
    if record.state not in {
        DemoExecutionState.DEMO_PROTECTION_PENDING,
        DemoExecutionState.DEMO_POSITION_OPEN,
        DemoExecutionState.DEMO_CLOSING,
        DemoExecutionState.DEMO_RECONCILIATION_REQUIRED,
        DemoExecutionState.DEMO_CLOSED,
        DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
    }:
        blockers.append("durable state cannot be terminalized from exact close evidence")
    entry_order_ids = {
        str(item.get("orderId") or "")
        for item in diagnosis.entry_order_history
        if item.get("orderId")
    }
    entry_fill_order_ids = {
        str(item.get("orderId") or "")
        for item in diagnosis.entry_executions
        if item.get("orderId")
    }
    if not diagnosis.entry_order_history or len(entry_order_ids) != 1:
        blockers.append("exact entry order is not attributed")
    if not diagnosis.entry_executions:
        blockers.append("exact entry fill is not attributed")
    if entry_order_ids != entry_fill_order_ids:
        blockers.append("entry order and fill attribution disagree")
    if record.order_id and entry_order_ids != {record.order_id}:
        blockers.append("persisted entry order ID conflicts with exchange evidence")
    if entry_quantity <= 0 or entry_quantity != record.accepted_quantity:
        blockers.append("entry filled quantity does not equal owned quantity")
    close_order_ids = {
        str(item.get("orderId") or "")
        for item in diagnosis.close_order_history
        if item.get("orderId")
    }
    close_fill_order_ids = {
        str(item.get("orderId") or "")
        for item in diagnosis.close_executions
        if item.get("orderId")
    }
    if not diagnosis.close_order_history or len(close_order_ids) != 1:
        blockers.append("exact close order is not attributed")
    if not diagnosis.close_executions:
        blockers.append("exact close fill is not attributed")
    if close_order_ids != close_fill_order_ids:
        blockers.append("close order and fill attribution disagree")
    if close_quantity <= 0 or close_quantity != record.accepted_quantity:
        blockers.append("attributed close quantity does not equal owned quantity")
    if active_symbol_position:
        blockers.append("remote position attributable to execution is not zero")
    if diagnosis.bot_owned_open_orders:
        blockers.append("bot-owned open order remains for execution")
    if diagnosis.close_source in {"take_profit", "stop_loss"}:
        if diagnosis.proposed_state != DemoExecutionState.DEMO_CLOSED:
            blockers.append("exchange TP/SL close does not propose DEMO_CLOSED")
    elif diagnosis.close_source == "manual_external_close":
        if diagnosis.proposed_state != DemoExecutionState.DEMO_CLOSED_EXTERNALLY:
            blockers.append(
                "manual external close does not propose DEMO_CLOSED_EXTERNALLY"
            )
    elif diagnosis.proposed_state != DemoExecutionState.DEMO_CLOSED:
        blockers.append("exact close source is not safely attributed")
    return list(dict.fromkeys(blockers))


def _authoritative_close_attribution(
    order: dict[str, Any], durable_reason: str | None,
) -> str | None:
    """Classify an exact exchange close from metadata, never from price."""
    return classify_exchange_close(order, durable_reason=durable_reason)


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
    all_records = repo.load_demo_executions()
    record = next(
        (item for item in all_records if str(item.id) == execution_id),
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
    second_positions = read_client.get_positions(record.symbol)
    second_open_orders = read_client.get_open_orders()
    closed_pnl = read_client.get_closed_pnl(record.symbol)
    transaction_loader = getattr(read_client, "get_transaction_log", None)
    transaction_log = transaction_loader() if callable(transaction_loader) else []

    entry_history = [
        item for item in history
        if _matches(item, record.order_id, record.order_link_id)
        and str(item.get("symbol") or "") == record.symbol.value
        and str(item.get("side") or "").upper() == record.side.value
    ]
    raw_close_history = [
        item for item in history
        if _matches(item, record.close_order_id, record.close_order_link_id)
    ]
    entry_fills = [
        item for item in executions
        if _matches(item, record.order_id, record.order_link_id)
        and str(item.get("symbol") or "") == record.symbol.value
        and str(item.get("side") or "").upper() == record.side.value
    ]
    raw_close_fills = [
        item for item in executions
        if _matches(item, record.close_order_id, record.close_order_link_id)
    ]
    entry_qty_for_match = sum(
        (_decimal(item.get("execQty")) for item in entry_fills), Decimal("0")
    )
    entry_time = min(
        (int(str(item.get("execTime") or "0")) for item in entry_fills),
        default=0,
    )
    used_order_ids = {
        value
        for other in all_records if str(other.id) != execution_id
        for value in [
            other.order_id, other.close_order_id,
            *(fill.order_id for fill in [*other.fills, *other.close_fills]),
        ]
        if value
    }
    used_exec_ids = {
        fill.execution_id
        for other in all_records if str(other.id) != execution_id
        for fill in [*other.fills, *other.close_fills]
    }
    selected_order_ids = {
        value for value in (
            record.order_id,
            record.close_order_id,
            *(fill.order_id for fill in [*record.fills, *record.close_fills]),
        ) if value
    }
    selected_exec_ids = {
        fill.execution_id for fill in [*record.fills, *record.close_fills]
        if fill.execution_id
    }
    conflicting_order_ids = sorted(selected_order_ids & used_order_ids)
    conflicting_execution_ids = sorted(selected_exec_ids & used_exec_ids)
    position_watermark = max(
        (int(str(item.get("updatedTime") or "0")) for item in positions),
        default=0,
    )
    close_history = [
        item for item in raw_close_history
        if _valid_close_order(
            item, entry_time, entry_qty_for_match, used_order_ids,
            position_watermark, record.symbol.value, record.side.value,
        )
    ]
    allowed_close_order_ids = {
        str(item.get("orderId") or "") for item in close_history
    }
    close_fills = [
        item for item in raw_close_fills
        if _valid_close_execution(
            item, entry_time, entry_qty_for_match, used_exec_ids,
            allowed_close_order_ids, position_watermark,
            record.symbol.value, record.side.value,
        )
    ]
    rejected_close_order_ids = [
        str(item.get("orderId") or "") for item in raw_close_history
        if item not in close_history and item.get("orderId")
    ]
    rejected_close_execution_ids = [
        str(item.get("execId") or "") for item in raw_close_fills
        if item not in close_fills and item.get("execId")
    ]
    transaction_log = [
        item for item in transaction_log
        if int(str(item.get("transactionTime") or item.get("createdTime") or "0"))
        >= entry_time
        and str(item.get("symbol") or record.symbol.value) == record.symbol.value
    ] if entry_time else []
    expected_close_side = "sell" if record.side.value == "BUY" else "buy"
    external_close_orders = [
        item for item in history
        if entry_time
        and int(str(item.get("createdTime") or item.get("updatedTime") or "0"))
        >= entry_time
        and str(item.get("side") or "").lower() == expected_close_side
        and str(item.get("orderStatus") or "") == "Filled"
        and str(item.get("reduceOnly") or "").lower() == "true"
        and Decimal("0") < _decimal(item.get("cumExecQty") or item.get("qty"))
        <= entry_qty_for_match
        and str(item.get("orderId") or "") not in used_order_ids
        and (not position_watermark or int(str(
            item.get("updatedTime") or item.get("createdTime") or "0"
        )) <= position_watermark)
    ]
    external_ids = {
        str(item.get("orderId") or "") for item in external_close_orders
    }
    external_close_fills = [
        item for item in executions
        if str(item.get("orderId") or "") in external_ids
        and str(item.get("side") or "").lower() == expected_close_side
        and str(item.get("execId") or "") not in used_exec_ids
        and int(str(item.get("execTime") or "0")) >= entry_time
    ]
    external_closed_pnl = [
        item for item in closed_pnl
        if str(item.get("orderId") or "") in external_ids
        and _decimal(item.get("closedSize") or item.get("qty"))
        == entry_qty_for_match
    ]
    close_source: str | None = (
        _authoritative_close_attribution(close_history[0], record.close_reason)
        if close_fills and close_history else None
    )
    if (
        not close_fills
        and len(external_close_orders) == 1
        and external_close_fills
        and external_closed_pnl
    ):
        close_history = external_close_orders
        close_fills = external_close_fills
        close_source = _authoritative_close_attribution(
            close_history[0], record.close_reason
        )
    known_links = {
        link for link in (record.order_link_id, record.close_order_link_id) if link
    }
    known_order_ids = {
        order_id for order_id in (record.order_id, record.close_order_id) if order_id
    }
    bot_orders = [
        item for item in open_orders
        if str(item.get("orderLinkId") or "") in known_links
        or str(item.get("orderId") or "") in known_order_ids
    ]
    unrelated = [item for item in open_orders if item not in bot_orders]
    active_positions = [item for item in positions if _decimal(item.get("size")) > 0]
    second_active_positions = [
        item for item in second_positions if _decimal(item.get("size")) > 0
    ]
    repeated_remote_flat = bool(
        not active_positions and not second_active_positions
        and not open_orders and not second_open_orders
    )

    blockers: list[str] = []
    if active_positions:
        blockers.append("remote position is not flat")
    if bot_orders:
        blockers.append("bot-owned open order exists")
    if record.order_id and not entry_history and not entry_fills:
        blockers.append("entry order is absent from authoritative history")
    if record.close_order_id and not close_history and not close_fills:
        blockers.append("close order is absent from authoritative history")
    if conflicting_order_ids:
        blockers.append("exchange order ID is assigned to another Demo execution")
    if conflicting_execution_ids:
        blockers.append("exchange execution ID is assigned to another Demo execution")
    if record.execution_environment != ExecutionEnvironment.BYBIT_DEMO:
        blockers.append("execution does not belong to BYBIT_DEMO")
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
    matching_closed_pnl = external_closed_pnl if close_source else [
        item for item in closed_pnl
        if record.close_order_id
        and str(item.get("orderId") or "") == record.close_order_id
    ]
    accounting = None
    if entry_fills and close_fills and close_history:
        try:
            accounting = calculate_fill_level_accounting(
                symbol=record.symbol,
                side=record.side,
                entry_order_id=str(entry_fills[0].get("orderId") or ""),
                close_order_id=str(close_fills[0].get("orderId") or ""),
                entry_fills=entry_fills,
                close_fills=close_fills,
                transaction_log=transaction_log,
                closed_pnl_rows=matching_closed_pnl,
            )
            gross = accounting.gross_price_pnl
            entry_fees = accounting.entry_fees
            close_fees = accounting.close_fees
            net = accounting.calculated_net_pnl
            if accounting.final:
                net = accounting.authoritative_closed_pnl or net
            else:
                blockers.append(
                    "canonical fill accounting is not finalized against "
                    "authoritative closed PnL"
                )
        except ValueError as exc:
            blockers.append(f"canonical fill accounting failed: {exc}")

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
        proposed = (
            DemoExecutionState.DEMO_CLOSED_EXTERNALLY
            if close_source == "manual_external_close"
            else DemoExecutionState.DEMO_CLOSED
            if close_source is not None
            else DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION
        )
    elif entry_qty >= record.requested_quantity:
        other_owners = [
            item for item in repo.load_demo_executions()
            if str(item.id) != execution_id
            and item.symbol == record.symbol
            and item.state not in {
                DemoExecutionState.DEMO_CLOSED,
                DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE,
                DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION,
                DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
                DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED,
                DemoExecutionState.DEMO_NOT_SUBMITTED,
                DemoExecutionState.DEMO_ORDER_CANCELLED,
            }
        ]
        if repeated_remote_flat and not other_owners and not unrelated:
            conclusion = "filled entry; close attribution unavailable; repeatedly flat"
            proposed = DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED
        else:
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
        transaction_log=transaction_log,
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
        close_source=close_source,
        repeated_remote_flat=repeated_remote_flat,
        rejected_close_order_ids=rejected_close_order_ids,
        rejected_close_execution_ids=rejected_close_execution_ids,
        conflicting_order_ids=conflicting_order_ids,
        conflicting_execution_ids=conflicting_execution_ids,
        funding_pnl=(
            accounting.funding_pnl if accounting is not None else Decimal("0")
        ),
        funding_transaction_ids=(
            list(accounting.funding_transaction_ids)
            if accounting is not None else []
        ),
        accounting_components=(
            accounting.payload() if accounting is not None else {}
        ),
        accounting_status=(
            accounting.status if accounting is not None else "UNAVAILABLE"
        ),
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
        and diagnosis.record.realized_exchange_pnl
        == diagnosis.net_realized_pnl
        and diagnosis.record.accounting_status == "FINAL"
    ):
        return True
    record = diagnosis.record.model_copy(deep=True)
    record.state = diagnosis.proposed_state
    record.failure_reason = (
        diagnosis.record.failure_reason
        if diagnosis.proposed_state == DemoExecutionState.DEMO_CLOSED
        else
        "position closed outside the bot workflow"
        if diagnosis.proposed_state == DemoExecutionState.DEMO_CLOSED_EXTERNALLY
        else "remote flat verified; closing execution attribution unavailable"
        if diagnosis.proposed_state == DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED
        else "Windows sleep/resume interrupted canary client workflow"
    )
    record.cleanup_result = "remote position flat and bot-owned orders zero"
    if record.order_id is None and diagnosis.entry_order_history:
        record.order_id = str(diagnosis.entry_order_history[0].get("orderId") or "") or None
    if diagnosis.close_executions:
        record.close_order_id = str(
            diagnosis.close_executions[0].get("orderId") or ""
        ) or None
        record.close_reason = diagnosis.close_source or "authoritative_exchange_close"
        record.exit_attribution = diagnosis.close_source
        order_evidence = diagnosis.close_order_history[0]
        record.exit_attribution_evidence = {
            "source": "read_only_exact_close_reconciliation",
            "order_id": record.close_order_id,
            "stop_order_type": str(order_evidence.get("stopOrderType") or "") or None,
            "create_type": str(order_evidence.get("createType") or "") or None,
            "reduce_only": str(order_evidence.get("reduceOnly") or "").lower() == "true",
            "close_on_trigger": str(order_evidence.get("closeOnTrigger") or "").lower() == "true",
            "owned_quantity": str(record.accepted_quantity),
        }
        record.attribution_failure_reason = None
        record.close_fills = [
            DemoFill(
                execution_id=str(item.get("execId") or ""),
                order_id=str(item.get("orderId") or ""),
                quantity=_decimal(item.get("execQty")),
                price=_decimal(item.get("execPrice")),
                fee=_decimal(item.get("execFee")),
                fee_currency=item.get("feeCurrency"),
                executed_at=_exchange_timestamp(item.get("execTime")),
            )
            for item in diagnosis.close_executions
        ]
        record.realized_exchange_pnl = diagnosis.net_realized_pnl
        record.gross_realized_pnl = diagnosis.gross_realized_pnl
        record.exchange_fees = diagnosis.entry_fees + diagnosis.close_fees
        record.entry_fees = diagnosis.entry_fees
        record.close_fees = diagnosis.close_fees
        record.funding_pnl = diagnosis.funding_pnl
        record.funding_transaction_ids = list(
            diagnosis.funding_transaction_ids
        )
        record.accounting_components = dict(diagnosis.accounting_components)
        record.accounting_status = diagnosis.accounting_status
        record.authoritative_closed_pnl = diagnosis.net_realized_pnl
        record.accounting_finalized_at = datetime.now(timezone.utc)
        record.paper_shadow_pnl = diagnosis.net_realized_pnl
        record.average_close_price = _weighted_average(diagnosis.close_executions)
        record.closed_at = max(
            _exchange_timestamp(item.get("execTime"))
            for item in diagnosis.close_executions
        )
    elif diagnosis.proposed_state == DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED:
        record.close_order_id = None
        record.close_order_link_id = None
        record.close_fills = []
        record.average_close_price = None
        record.realized_exchange_pnl = None
        record.close_reason = "close_attribution_unavailable"
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
        "close_source": diagnosis.close_source,
        "rejected_close_order_ids": diagnosis.rejected_close_order_ids,
        "rejected_close_execution_ids": diagnosis.rejected_close_execution_ids,
        "conflicting_order_ids": diagnosis.conflicting_order_ids,
        "conflicting_execution_ids": diagnosis.conflicting_execution_ids,
        "accounting_status": diagnosis.accounting_status,
        "accounting_components": diagnosis.accounting_components,
    }
    event_types = []
    if diagnosis.rejected_close_order_ids or diagnosis.rejected_close_execution_ids:
        event_types.append("HISTORICAL_CLOSE_ATTRIBUTION_REJECTED")
    event_types += [
        "REMOTE_POSITION_FLAT_VERIFIED",
        "CLOSE_ATTRIBUTION_UNAVAILABLE",
        "EXECUTION_FINALIZED_FLAT_VERIFIED",
    ] if diagnosis.proposed_state == DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED else [
        "READ_ONLY_RECONCILIATION_COMPLETED",
        "EXACT_CLOSE_ATTRIBUTED"
        if diagnosis.proposed_state == DemoExecutionState.DEMO_CLOSED
        else
        "EXTERNAL_CLOSE_ATTRIBUTED"
        if diagnosis.proposed_state == DemoExecutionState.DEMO_CLOSED_EXTERNALLY
        else "EXECUTION_REPAIR_APPLIED",
        "FINAL_REMOTE_STATE_FLAT",
    ]
    if (
        diagnosis.accounting_status == "FINAL"
        and diagnosis.close_executions
        and (
        diagnosis.record.realized_exchange_pnl
        != diagnosis.net_realized_pnl
        or diagnosis.record.accounting_status != "FINAL"
        )
    ):
        event_types.append("PNL_ACCOUNTING_RECONCILED")
    return repository.repair_demo_execution(
        record,
        event_types=event_types,
        repair_payload=repair_payload,
    )


def diagnosis_payload(diagnosis: DemoExecutionDiagnosis) -> dict[str, Any]:
    record = diagnosis.record
    return {
        "execution_id": str(record.id),
        "candidate_id": str(record.candidate_id),
        "risk_decision_id": record.risk_decision_id,
        "run_id": record.run_id,
        "execution_environment": record.execution_environment.value,
        "symbol": record.symbol.value,
        "side": record.side.value,
        "order_link_id": record.order_link_id,
        "close_order_link_id": record.close_order_link_id,
        "entry_order_id": record.order_id or (
            str(diagnosis.entry_order_history[0].get("orderId") or "")
            if diagnosis.entry_order_history else None
        ),
        "close_order_id": (
            str(diagnosis.close_order_history[0].get("orderId") or "")
            if diagnosis.close_order_history else None
        ) or record.close_order_id,
        "durable_state": record.state.value,
        "durable_transitions": diagnosis.durable_events,
        "quantity_requested": str(record.requested_quantity),
        "quantity_executed": str(sum((_decimal(x.get("execQty")) for x in diagnosis.entry_executions), Decimal("0"))),
        "entry_filled_quantity": str(sum((_decimal(x.get("execQty")) for x in diagnosis.entry_executions), Decimal("0"))),
        "attributed_close_filled_quantity": str(sum((_decimal(x.get("execQty")) for x in diagnosis.close_executions), Decimal("0"))),
        "entry_fill_ids": [str(item.get("execId") or "") for item in diagnosis.entry_executions],
        "close_fill_ids": [str(item.get("execId") or "") for item in diagnosis.close_executions],
        "entry_average_price": str(_weighted_average(diagnosis.entry_executions)),
        "close_average_price": str(_weighted_average(diagnosis.close_executions)),
        "entry_fees": str(diagnosis.entry_fees),
        "close_fees": str(diagnosis.close_fees),
        "close_source": diagnosis.close_source,
        "repeated_remote_flat": diagnosis.repeated_remote_flat,
        "rejected_close_order_ids": diagnosis.rejected_close_order_ids,
        "rejected_close_execution_ids": diagnosis.rejected_close_execution_ids,
        "conflicting_order_ids": diagnosis.conflicting_order_ids,
        "conflicting_execution_ids": diagnosis.conflicting_execution_ids,
        "gross_realized_pnl": str(diagnosis.gross_realized_pnl),
        "net_realized_pnl": str(diagnosis.net_realized_pnl),
        "funding_pnl": str(diagnosis.funding_pnl),
        "funding_transaction_ids": diagnosis.funding_transaction_ids,
        "accounting_status": diagnosis.accounting_status,
        "accounting_components": diagnosis.accounting_components,
        "tp_sl_attempts": [event for event in diagnosis.durable_events if event["event_type"] in {"PROTECTION_PENDING", "DEMO_POSITION_OPEN"}],
        "cleanup_attempts": [event for event in diagnosis.durable_events if event["event_type"] in {"CLOSE_SUBMITTING", "CLOSE_ACK", "CLOSE_Filled"}],
        "entry_order_history": _json_safe(diagnosis.entry_order_history),
        "close_order_history": _json_safe(diagnosis.close_order_history),
        "entry_executions": _json_safe(diagnosis.entry_executions),
        "close_executions": _json_safe(diagnosis.close_executions),
        "transaction_log": _json_safe(diagnosis.transaction_log),
        "final_remote_position": _json_safe(diagnosis.remote_positions),
        "final_remote_open_orders": _json_safe(diagnosis.remote_open_orders),
        "remaining_bot_owned_open_orders": _json_safe(diagnosis.bot_owned_open_orders),
        "unrelated_open_orders": _json_safe(diagnosis.unrelated_open_orders),
        "conclusion": diagnosis.conclusion,
        "proposed_terminal_state": diagnosis.proposed_state.value if diagnosis.proposed_state else None,
        "can_reconcile": diagnosis.repairable,
        "repairable": diagnosis.repairable,
        "blockers": diagnosis.blockers,
    }


def _matches(item: dict[str, Any], order_id: str | None, link_id: str | None) -> bool:
    return bool(
        (order_id and str(item.get("orderId") or "") == order_id)
        or (link_id and str(item.get("orderLinkId") or "") == link_id)
    )


def _valid_close_order(
    item: dict[str, Any], entry_time: int, owned_quantity: Decimal,
    used_order_ids: set[str], position_watermark: int,
    symbol: str, entry_side: str,
) -> bool:
    timestamp = int(str(item.get("updatedTime") or item.get("createdTime") or "0"))
    quantity = _decimal(item.get("cumExecQty") or item.get("qty"))
    return bool(
        entry_time and timestamp >= entry_time
        and str(item.get("symbol") or "") == symbol
        and str(item.get("side") or "").upper()
        == ("SELL" if entry_side == "BUY" else "BUY")
        and (not position_watermark or timestamp <= position_watermark)
        and str(item.get("orderId") or "") not in used_order_ids
        and str(item.get("reduceOnly") or "").lower() == "true"
        and Decimal("0") < quantity <= owned_quantity
    )


def _valid_close_execution(
    item: dict[str, Any], entry_time: int, owned_quantity: Decimal,
    used_exec_ids: set[str], allowed_order_ids: set[str],
    position_watermark: int, symbol: str, entry_side: str,
) -> bool:
    timestamp = int(str(item.get("execTime") or "0"))
    quantity = _decimal(item.get("execQty"))
    return bool(
        entry_time and timestamp >= entry_time
        and str(item.get("symbol") or "") == symbol
        and str(item.get("side") or "").upper()
        == ("SELL" if entry_side == "BUY" else "BUY")
        and (not position_watermark or timestamp <= position_watermark)
        and str(item.get("execId") or "") not in used_exec_ids
        and str(item.get("orderId") or "") in allowed_order_ids
        and Decimal("0") < quantity <= owned_quantity
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


def _exchange_timestamp(value: object) -> datetime:
    return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc)


def _json_safe(value: object) -> object:
    return json.loads(json.dumps(value, default=str))
