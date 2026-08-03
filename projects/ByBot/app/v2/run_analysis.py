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


class _StableReadSnapshotClient:
    """Reuse immutable read-only exchange evidence during final analysis.

    A completed run may contain many executions for the same symbol. Repeating
    the same paginated history, fill, PnL and transaction-log requests for
    every execution can exceed the runner's bounded final-analysis deadline.
    Mutable safety state (positions and open orders) is cached only after two
    consecutive identical authoritative reads.
    """

    def __init__(
        self,
        client: ReadOnlyBybitDemoClient,
        universe_symbols: tuple[str, ...] = (),
    ) -> None:
        self._client = client
        self._universe_symbols = universe_symbols
        self._verified = False
        self._immutable: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._stable: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._previous: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def verify(self) -> None:
        if not self._verified:
            self._client.verify()
            self._verified = True

    def get_order_history(self, symbol: Any) -> list[dict[str, Any]]:
        return self._immutable_rows(
            "order_history", symbol.value, lambda: self._client.get_order_history(symbol)
        )

    def get_executions(self, symbol: Any) -> list[dict[str, Any]]:
        return self._immutable_rows(
            "executions", symbol.value, lambda: self._client.get_executions(symbol)
        )

    def get_closed_pnl(self, symbol: Any) -> list[dict[str, Any]]:
        return self._immutable_rows(
            "closed_pnl", symbol.value, lambda: self._client.get_closed_pnl(symbol)
        )

    def get_transaction_log(self) -> list[dict[str, Any]]:
        return self._immutable_rows(
            "transaction_log", "USDT", self._client.get_transaction_log
        )

    def get_positions(self, symbol: Any) -> list[dict[str, Any]]:
        return self._stable_rows(
            "positions", symbol.value, lambda: self._client.get_positions(symbol)
        )

    def get_usdt_positions(self) -> list[dict[str, Any]]:
        loader = getattr(self._client, "get_usdt_positions", None)
        if callable(loader):
            return self._stable_rows("usdt_positions", "USDT", loader)
        return [
            row
            for symbol in self._configured_symbols()
            for row in self.get_positions(symbol)
        ]

    def get_open_orders(self) -> list[dict[str, Any]]:
        return self._stable_rows(
            "open_orders", "USDT", self._client.get_open_orders
        )

    def _configured_symbols(self) -> list[Any]:
        from app.models import Symbol

        return [Symbol(value) for value in self._universe_symbols]

    def _immutable_rows(
        self,
        kind: str,
        scope: str,
        loader: Any,
    ) -> list[dict[str, Any]]:
        key = (kind, scope)
        if key not in self._immutable:
            self._immutable[key] = list(loader())
        return list(self._immutable[key])

    def _stable_rows(
        self,
        kind: str,
        scope: str,
        loader: Any,
    ) -> list[dict[str, Any]]:
        key = (kind, scope)
        if key in self._stable:
            return list(self._stable[key])
        current = list(loader())
        previous = self._previous.get(key)
        if previous is not None and _canonical_rows(previous) == _canonical_rows(current):
            self._stable[key] = current
        self._previous[key] = current
        return list(current)


def _canonical_rows(rows: list[dict[str, Any]]) -> str:
    return json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)


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
    raw_client = client or ReadOnlyBybitDemoClient(
        config.api_key, config.api_secret, base_url=config.rest_url
    )
    read_client = _StableReadSnapshotClient(raw_client, config.universe_symbols)
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
    run_state_loader = getattr(repo, "load_v2_run_state", None)
    durable_run_state = (
        dict(run_state_loader(run_id) or {})
        if callable(run_state_loader)
        else {}
    )
    durable_runtime = dict(durable_run_state.get("runtime") or {})
    artifact_run_phase = next((
        value.get("run_phase")
        or (value.get("run_finalization") or {}).get("phase")
        for value in artifact_inputs.values()
        if isinstance(value, dict)
        and (
            value.get("run_phase")
            or (value.get("run_finalization") or {}).get("phase")
        )
    ), None)
    artifact_cycle_failures = next((
        value.get("total_cycle_failures")
        for value in artifact_inputs.values()
        if isinstance(value, dict)
        and value.get("total_cycle_failures") is not None
    ), None)
    durable_run_phase = (
        (durable_runtime.get("run_finalization") or {}).get("phase")
        or durable_runtime.get("run_phase")
        or durable_run_state.get("status")
    )
    durable_cycle_failures = _durable_cycle_failures(durable_runtime)

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
    authoritative_trades: list[dict[str, Any]] = []
    completed_analysis_errors: list[dict[str, str]] = []
    for record in completed:
        try:
            diagnosis = diagnose_demo_execution(
                config, str(record.id), repository=repo, client=read_client
            )
            blockers = exact_close_reconciliation_blockers(diagnosis)
            if blockers or diagnosis.close_source is None:
                completed_analysis_errors.append({
                    "execution_id": str(record.id),
                    "error": "; ".join(blockers or [
                        "exact close attribution is unavailable"
                    ]),
                })
                continue
            authoritative_trades.append({
                "execution_id": str(record.id),
                "candidate_id": str(record.candidate_id),
                "run_id": record.run_id,
                "symbol": record.symbol.value,
                "side": record.side.value,
                "strategy": record.strategy_name,
                "entry_order_id": record.order_id,
                "close_order_id": (
                    str(diagnosis.close_order_history[0].get("orderId") or "")
                    if diagnosis.close_order_history else record.close_order_id
                ),
                "entry_execution_ids": [
                    str(item.get("execId") or "")
                    for item in diagnosis.entry_executions
                ],
                "close_execution_ids": [
                    str(item.get("execId") or "")
                    for item in diagnosis.close_executions
                ],
                "entry_price": str(_weighted_execution_price(
                    diagnosis.entry_executions
                )),
                "exit_price": str(_weighted_execution_price(
                    diagnosis.close_executions
                )),
                "exit_attribution": diagnosis.close_source,
                "gross_realized_pnl": str(diagnosis.gross_realized_pnl),
                "entry_fees": str(diagnosis.entry_fees),
                "close_fees": str(diagnosis.close_fees),
                "net_realized_pnl": str(diagnosis.net_realized_pnl),
                "source": "authoritative_exchange_order_and_execution_ids",
            })
        except Exception as exc:
            completed_analysis_errors.append({
                "execution_id": str(record.id),
                "error": type(exc).__name__,
            })
    net_pnl = sum(
        (Decimal(item["net_realized_pnl"]) for item in authoritative_trades),
        Decimal("0"),
    )
    unattributed_exit_count = sum(
        item["exit_attribution"] == "unattributed_external_close"
        for item in authoritative_trades
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
        "authoritative_trades": authoritative_trades,
        "unattributed_exit_count": unattributed_exit_count,
        "completed_trade_analysis_errors": completed_analysis_errors,
        "unresolved_execution_ids": [str(item.id) for item in unresolved],
        "remote_open_positions": remote_positions,
        "remote_bot_owned_open_orders": len(diagnostics.bot_owned_open_orders),
        "remote_unrelated_open_orders": len(diagnostics.unrelated_open_orders),
        "diagnostics_passed": diagnostics.passed,
        "artifact_inputs_found": sorted(artifact_inputs),
        "artifact_run_phase": artifact_run_phase or durable_run_phase,
        "artifact_cycle_failures": (
            artifact_cycle_failures
            if artifact_cycle_failures is not None
            else durable_cycle_failures
        ),
        "run_phase_source": (
            "artifact" if artifact_run_phase is not None
            else "durable_v2_runs" if durable_run_phase is not None
            else None
        ),
        "cycle_failures_source": (
            "artifact" if artifact_cycle_failures is not None
            else "durable_v2_runs"
            if durable_cycle_failures is not None
            else None
        ),
        "durable_run_state_found": bool(durable_run_state),
    }
    summary = {
        **status,
        "analysis_result": (
            "PASS"
            if not unresolved
            and not remote_positions
            and not diagnostics.bot_owned_open_orders
            and diagnostics.passed
            and len(authoritative_trades) == len(completed)
            and not completed_analysis_errors
            and unattributed_exit_count == 0
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


def _durable_cycle_failures(runtime: dict[str, Any]) -> int | None:
    value = runtime.get("total_cycle_failures")
    if value is not None:
        return int(value)
    occurrences = runtime.get("failure_occurrences")
    if isinstance(occurrences, dict):
        return sum(int(count or 0) for count in occurrences.values())
    metrics = runtime.get("symbol_cycle_metrics")
    if isinstance(metrics, dict):
        return sum(
            int((item or {}).get("cycles_failed") or 0)
            for item in metrics.values()
            if isinstance(item, dict)
        )
    return None


def _weighted_execution_price(rows: list[dict[str, Any]]) -> Decimal:
    quantity = sum(
        (Decimal(str(item.get("execQty") or "0")) for item in rows), Decimal("0")
    )
    if quantity <= 0:
        return Decimal("0")
    value = sum(
        (
            Decimal(str(item.get("execQty") or "0"))
            * Decimal(str(item.get("execPrice") or "0"))
            for item in rows
        ),
        Decimal("0"),
    )
    return value / quantity


def _markdown(summary: dict[str, Any]) -> str:
    unresolved = summary["unresolved"]
    lines = [
        f"# Demo V2 analysis: {summary['run_id']}", "",
        f"- Result: **{summary['analysis_result']}**",
        f"- Completed trades: {summary['completed_trades']}",
        f"- Net realized PnL: {summary['net_realized_pnl']}",
        f"- Unattributed exits: {summary['unattributed_exit_count']}",
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
    lines.extend(["", "## Authoritative completed trades", ""])
    if not summary["authoritative_trades"]:
        lines.append("None.")
    for row in summary["authoritative_trades"]:
        lines.extend([
            f"### {row['execution_id']}", "",
            f"- Symbol: {row['symbol']}",
            f"- Exit attribution: {row['exit_attribution']}",
            f"- Entry / exit: {row['entry_price']} / {row['exit_price']}",
            f"- Net realized PnL: {row['net_realized_pnl']}", "",
        ])
    if summary["completed_trade_analysis_errors"]:
        lines.extend(["## Completed-trade analysis errors", ""])
        for row in summary["completed_trade_analysis_errors"]:
            lines.append(f"- {row['execution_id']}: {row['error']}")
    return "\n".join(lines).rstrip() + "\n"
