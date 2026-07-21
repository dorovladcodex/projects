from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any

from app.bybit.demo_diagnostics import (
    RESOLVED_STATES,
    DemoDiagnosticsConfig,
    ReadOnlyBybitDemoClient,
    format_demo_diagnostics,
    run_demo_diagnostics,
)
from app.bybit.demo_execution_recovery import (
    diagnose_demo_execution,
    exact_close_reconciliation_blockers,
)
from app.db.persistence import PersistenceRepository


_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMPLETED_TRADE_STATES = {
    "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE",
    "DEMO_CLOSED_AFTER_INTERRUPTION", "DEMO_CLOSED_EXTERNALLY",
    "DEMO_FAILED_FLAT_VERIFIED",
}


def analyze_demo_v2_run(
    run_id: str,
    config: DemoDiagnosticsConfig,
    *,
    repository: PersistenceRepository | None = None,
    client: ReadOnlyBybitDemoClient | None = None,
    artifact_root: str | Path = "artifacts/demo-v2",
) -> dict[str, Any]:
    """Build a read-only durable/remote reconciliation report for one run."""
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id contains unsafe characters")
    repo = repository or PersistenceRepository(config.database_url, create_schema=False)
    if not repo.available:
        raise RuntimeError("database persistence is unavailable")
    read_client = client or ReadOnlyBybitDemoClient(
        config.api_key, config.api_secret, base_url=config.rest_url
    )
    diagnostics = run_demo_diagnostics(
        config, repository=repo, client=read_client
    )
    all_records = repo.load_demo_executions()
    records = [record for record in all_records if record.run_id == run_id]
    unresolved = [record for record in records if record.state not in RESOLVED_STATES]
    analysis_dir = Path(artifact_root) / run_id / "analysis"
    run_dir = analysis_dir.parent
    dry_run_dir = analysis_dir / "dry-runs"
    dry_run_dir.mkdir(parents=True, exist_ok=True)
    artifact_inputs = {
        name: _read_json(run_dir / name)
        for name in ("summary.json", "runner-report.json")
        if (run_dir / name).exists()
    }

    recovery_rows: list[dict[str, Any]] = []
    for record in unresolved:
        diagnosis = diagnose_demo_execution(
            config, str(record.id), repository=repo, client=read_client
        )
        blockers = exact_close_reconciliation_blockers(diagnosis)
        row = {
            "execution_id": str(record.id),
            "durable_state": record.state.value,
            "conclusion": diagnosis.conclusion,
            "proposed_terminal_state": (
                diagnosis.proposed_state.value if diagnosis.proposed_state else None
            ),
            "close_source": diagnosis.close_source,
            "exact_blocking_invariants": blockers,
            "read_only_repair_eligible": bool(
                diagnosis.proposed_state is not None and not blockers
            ),
            "remote_position_flat": not any(
                Decimal(str(item.get("size") or "0")) > 0
                for item in diagnosis.remote_positions
            ),
            "bot_owned_open_orders": len(diagnosis.bot_owned_open_orders),
            "unrelated_open_orders": len(diagnosis.unrelated_open_orders),
            "entry_order_ids": sorted({
                str(item.get("orderId")) for item in diagnosis.entry_order_history
                if item.get("orderId")
            }),
            "close_order_ids": sorted({
                str(item.get("orderId")) for item in diagnosis.close_order_history
                if item.get("orderId")
            }),
            "entry_execution_ids": sorted({
                str(item.get("execId")) for item in diagnosis.entry_executions
                if item.get("execId")
            }),
            "close_execution_ids": sorted({
                str(item.get("execId")) for item in diagnosis.close_executions
                if item.get("execId")
            }),
        }
        recovery_rows.append(row)
        _write_json(dry_run_dir / f"{record.id}.json", row)

    completed = [
        record for record in records
        if record.state.value in _COMPLETED_TRADE_STATES
        and record.accepted_quantity > 0
    ]
    net_pnl = sum(
        (
            record.realized_exchange_pnl
            for record in completed
            if record.realized_exchange_pnl is not None
        ),
        Decimal("0"),
    )
    remote_positions = {
        symbol: value for symbol, value in diagnostics.positions.items()
        if Decimal(str(value.get("size") or "0")) > 0
    }
    status = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "durable_execution_count": len(records),
        "completed_trades": len(completed),
        "net_realized_pnl": str(net_pnl),
        "unresolved_execution_ids": [str(item.id) for item in unresolved],
        "remote_open_positions": remote_positions,
        "remote_bot_owned_open_orders": len(diagnostics.bot_owned_open_orders),
        "remote_unrelated_open_orders": len(diagnostics.unrelated_open_orders),
        "diagnostics_passed": diagnostics.passed,
        "artifact_inputs_found": sorted(artifact_inputs),
        "artifact_run_phase": next((
            value.get("run_phase")
            or (value.get("run_finalization") or {}).get("phase")
            for value in artifact_inputs.values()
            if isinstance(value, dict)
            and (
                value.get("run_phase")
                or (value.get("run_finalization") or {}).get("phase")
            )
        ), None),
        "artifact_cycle_failures": next((
            value.get("total_cycle_failures")
            for value in artifact_inputs.values()
            if isinstance(value, dict)
            and value.get("total_cycle_failures") is not None
        ), None),
    }
    summary = {
        **status,
        "analysis_result": (
            "PASS"
            if not unresolved
            and not remote_positions
            and not diagnostics.bot_owned_open_orders
            and diagnostics.passed
            else "FAIL"
        ),
        "unresolved": recovery_rows,
        "mutation_attempted": False,
    }
    _write_json(analysis_dir / "summary.json", summary)
    _write_json(analysis_dir / "status.json", status)
    _write_json(analysis_dir / "unresolved.json", recovery_rows)
    (analysis_dir / "diagnostics.txt").write_text(
        format_demo_diagnostics(diagnostics) + "\n", encoding="utf-8"
    )
    with (analysis_dir / "relevant-events.jsonl").open(
        "w", encoding="utf-8", newline="\n"
    ) as stream:
        for record in records:
            for event in repo.load_demo_execution_events(str(record.id)):
                stream.write(json.dumps({
                    "execution_id": str(record.id), **event,
                }, ensure_ascii=False, default=str) + "\n")
    (analysis_dir / "analysis.md").write_text(
        _markdown(summary), encoding="utf-8"
    )
    summary["analysis_directory"] = str(analysis_dir.resolve())
    summary["analysis_markdown"] = str((analysis_dir / "analysis.md").resolve())
    return summary


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"artifact_read_error": type(exc).__name__}


def _markdown(summary: dict[str, Any]) -> str:
    unresolved = summary["unresolved"]
    lines = [
        f"# Demo V2 analysis: {summary['run_id']}", "",
        f"- Result: **{summary['analysis_result']}**",
        f"- Completed trades: {summary['completed_trades']}",
        f"- Net realized PnL: {summary['net_realized_pnl']}",
        f"- Unresolved executions: {len(unresolved)}",
        f"- Remote open positions: {len(summary['remote_open_positions'])}",
        f"- Remote bot-owned orders: {summary['remote_bot_owned_open_orders']}",
        "- Exchange/database mutation attempted: false", "",
        "## Exact unresolved invariants", "",
    ]
    if not unresolved:
        lines.append("None.")
    for row in unresolved:
        blockers = row["exact_blocking_invariants"] or [
            "durable execution has not yet been terminalized"
        ]
        lines.extend([
            f"### {row['execution_id']}", "",
            f"- Durable state: {row['durable_state']}",
            f"- Proposed state: {row['proposed_terminal_state']}",
            f"- Read-only repair eligible: {str(row['read_only_repair_eligible']).lower()}",
            f"- Blockers: {'; '.join(blockers)}", "",
        ])
    return "\n".join(lines).rstrip() + "\n"
