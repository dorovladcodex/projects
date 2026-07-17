from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bybit.demo_diagnostics import DemoDiagnosticsConfig  # noqa: E402
from app.bybit.demo_execution_recovery import diagnose_demo_execution  # noqa: E402
from app.db.persistence import PersistenceRepository  # noqa: E402


def _proven_attribution(order: dict[str, object]) -> str | None:
    stop_type = str(order.get("stopOrderType") or "").casefold()
    create_type = str(order.get("createType") or "").casefold()
    if stop_type == "stoploss" and create_type == "createbystoploss":
        return "exchange_generated_sl"
    if stop_type == "takeprofit" and create_type == "createbytakeprofit":
        return "exchange_generated_tp"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run-first repair of one proven V2 Demo exit attribution."
    )
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    config = DemoDiagnosticsConfig.load()
    diagnosis = diagnose_demo_execution(config, args.execution_id)
    close_orders = diagnosis.close_order_history
    close_fills = diagnosis.close_executions
    order = close_orders[0] if len(close_orders) == 1 else None
    attribution = _proven_attribution(order or {})
    remote_flat = all(str(item.get("size") or "0") in {"", "0", "0.0"} for item in diagnosis.remote_positions)
    evidence_complete = bool(
        attribution
        and order
        and str(order.get("orderStatus") or "") == "Filled"
        and bool(order.get("reduceOnly"))
        and bool(order.get("closeOnTrigger"))
        and len(close_fills) >= 1
        and remote_flat
        and not diagnosis.bot_owned_open_orders
        and not diagnosis.blockers
    )
    evidence = {
        "entry_order_id": diagnosis.record.order_id,
        "close_order_id": str((order or {}).get("orderId") or "") or None,
        "entry_execution_ids": [str(item.get("execId") or "") for item in diagnosis.entry_executions],
        "exit_execution_ids": [str(item.get("execId") or "") for item in close_fills],
        "stop_order_type": (order or {}).get("stopOrderType"),
        "create_type": (order or {}).get("createType"),
        "trigger_price": (order or {}).get("triggerPrice"),
        "reduce_only": bool((order or {}).get("reduceOnly")),
        "close_on_trigger": bool((order or {}).get("closeOnTrigger")),
        "order_status": (order or {}).get("orderStatus"),
        "remote_flat": remote_flat,
        "bot_owned_open_orders": len(diagnosis.bot_owned_open_orders),
    }
    output = {
        "mode": "APPLY" if args.apply else "DRY_RUN",
        "execution_id": args.execution_id,
        "current_exit_attribution": diagnosis.record.exit_attribution,
        "proven_exit_attribution": attribution,
        "evidence_complete": evidence_complete,
        "evidence": evidence,
        "mutation_applied": False,
    }
    if not evidence_complete:
        output["refusal_reason"] = "authoritative exchange attribution evidence is incomplete"
        print(json.dumps(output, indent=2))
        return 1
    if args.apply:
        repository = PersistenceRepository(config.database_url, create_schema=False)
        output["mutation_applied"] = repository.update_demo_exit_attribution(
            args.execution_id, attribution=attribution, evidence=evidence
        )
        if not output["mutation_applied"]:
            output["refusal_reason"] = "durable analytics repair failed"
            print(json.dumps(output, indent=2))
            return 1
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
