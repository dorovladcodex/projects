from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bybit.demo_diagnostics import (  # noqa: E402
    DemoDiagnosticsConfig,
    run_demo_diagnostics,
)
from app.bybit.demo_execution_recovery import diagnose_demo_execution  # noqa: E402
from app.db.persistence import PersistenceRepository  # noqa: E402
from app.models import DemoExecutionState  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Append one guarded Demo incident link")
    parser.add_argument("--activation-id", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--confirm-link", action="store_true")
    args = parser.parse_args()
    try:
        config = DemoDiagnosticsConfig.load()
        repository = PersistenceRepository(config.database_url, create_schema=False)
        diagnostics = run_demo_diagnostics(config, repository=repository)
        execution = next(
            (item for item in repository.load_demo_executions()
             if str(item.id) == args.execution_id), None
        )
        activation = next(
            (event for event in diagnostics.kill_switch.get("events") or []
             if str(event.get("id") or "") == args.activation_id), None
        )
        diagnosis = diagnose_demo_execution(
            config, args.execution_id, repository=repository
        )
        blockers: list[str] = []
        if execution is None or activation is None:
            blockers.append("activation or execution was not found")
        else:
            if execution.run_id != args.run_id:
                blockers.append("run ID mismatch")
            if execution.symbol.value != "BTCUSDT":
                blockers.append("symbol mismatch")
            if execution.state != DemoExecutionState.DEMO_CLOSED_EXTERNALLY:
                blockers.append("execution is not externally closed")
            if activation.get("event_type") != "KILL_SWITCH_ACTIVATED":
                blockers.append("event is not a kill-switch activation")
            if str(activation.get("execution_id") or ""):
                blockers.append("activation already has an execution link")
            activated_at = activation.get("created_at")
            if isinstance(activated_at, str):
                activated_at = datetime.fromisoformat(activated_at.replace("Z", "+00:00"))
            if not (execution.created_at <= activated_at <= execution.updated_at):
                blockers.append("activation timestamp is outside execution window")
            if "unattributed active Demo order for BTCUSDT" not in activation.get("reasons", []):
                blockers.append("activation reason does not match restart incident")
        if diagnosis.blockers:
            blockers.extend(diagnosis.blockers)
        if diagnosis.close_source not in {
            "external_or_exchange_triggered_reduce_only",
            "authoritative_exchange_close",
        }:
            blockers.append("authoritative close source is unavailable")
        if not diagnosis.entry_order_history or not diagnosis.close_order_history:
            blockers.append("entry or close order history is unavailable")
        if not any(
            str(item.get("orderStatus") or "") == "Filled"
            and str(item.get("reduceOnly") or "").lower() == "true"
            for item in diagnosis.close_order_history
        ):
            blockers.append("terminal reduce-only close was not verified")
        audit = {event.get("event_type") for event in diagnosis.durable_events}
        if not {
            "READ_ONLY_RECONCILIATION_COMPLETED",
            "EXTERNAL_CLOSE_ATTRIBUTED",
            "FINAL_REMOTE_STATE_FLAT",
        }.issubset(audit):
            blockers.append("terminal repair evidence is incomplete")
        if diagnostics.bot_owned_open_orders or diagnostics.unrelated_open_orders:
            blockers.append("remote open orders are not zero")
        if diagnostics.unresolved_executions:
            blockers.append("unresolved Demo execution exists")
        if any(position["size"] != "0" for position in diagnostics.positions.values()):
            blockers.append("remote position is not flat")
        if blockers:
            print("KILL-SWITCH ACTIVATION LINK: REFUSED", file=sys.stderr)
            for blocker in dict.fromkeys(blockers):
                print(f"- {blocker}", file=sys.stderr)
            return 1
        print(f"ACTIVATION ID: {args.activation_id}")
        print(f"EXECUTION ID: {args.execution_id}")
        print(f"RUN ID: {args.run_id}")
        print("LINK EVIDENCE: PASS")
        if not args.confirm_link:
            print("KILL-SWITCH ACTIVATION LINK: DRY RUN")
            return 0
        evidence = {
            "symbol": "BTCUSDT",
            "activation_created_at": str(activation.get("created_at")),
            "activation_reason": "unattributed active Demo order for BTCUSDT",
            "execution_created_at": execution.created_at.isoformat(),
            "execution_updated_at": execution.updated_at.isoformat(),
            "entry_order_id": execution.order_id,
            "close_order_id": execution.close_order_id,
            "entry_order_terminal": True,
            "close_order_terminal_reduce_only": True,
            "remote_positions_flat": True,
            "remote_open_orders_zero": True,
            "terminal_repair_evidence": sorted(audit),
        }
        if not repository.repair_demo_kill_switch_activation_link(
            activation_id=args.activation_id,
            execution_id=args.execution_id,
            run_id=args.run_id,
            evidence=evidence,
        ):
            print("KILL-SWITCH ACTIVATION LINK: FAIL", file=sys.stderr)
            return 1
        print("KILL-SWITCH ACTIVATION LINK: PASS")
        return 0
    except Exception as exc:
        print(f"KILL-SWITCH ACTIVATION LINK: FAIL\nERROR: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
