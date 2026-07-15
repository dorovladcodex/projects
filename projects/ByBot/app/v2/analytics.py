from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any


TERMINAL_STATES = {
    "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE", "DEMO_CLOSED_AFTER_INTERRUPTION",
    "DEMO_CLOSED_EXTERNALLY", "DEMO_FAILED_FLAT_VERIFIED", "DEMO_ORDER_CANCELLED",
    "DEMO_NOT_SUBMITTED",
}


class V2ReportGenerator:
    def __init__(self, repository: Any, base_directory: str) -> None:
        self.repository = repository
        self.base_directory = Path(base_directory)

    def generate(self, run_id: str) -> dict[str, Any]:
        rows = self.repository.v2_report_rows(run_id)
        signals = rows["signals"]
        rejections = rows["rejections"]
        executions = rows["executions"]
        incidents = rows["incidents"]
        # Repository query is run-scoped; assert again to prevent report leakage.
        for collection in (signals, rejections, executions, incidents):
            if any(str(item.get("run_id")) != run_id for item in collection):
                raise ValueError("report data contains a different run_id")
        directory = self.base_directory / run_id
        directory.mkdir(parents=True, exist_ok=True)
        trades = [item for item in executions if str(item.get("state")) in TERMINAL_STATES]
        summary = self._summary(run_id, signals, rejections, executions, trades, incidents)
        _write_json(directory / "summary.json", summary)
        _write_json(directory / "incidents.json", incidents)
        _write_csv(directory / "signals.csv", [_signal_row(item) for item in signals])
        _write_csv(directory / "rejections.csv", [_rejection_row(item) for item in rejections])
        _write_csv(directory / "trades.csv", [_trade_row(item) for item in trades])
        return {**summary, "artifact_directory": str(directory.resolve())}

    @staticmethod
    def _summary(
        run_id: str, signals: list[dict[str, Any]], rejections: list[dict[str, Any]],
        executions: list[dict[str, Any]], trades: list[dict[str, Any]],
        incidents: list[dict[str, Any]],
    ) -> dict[str, Any]:
        pnl = [_d(item.get("realized_exchange_pnl")) for item in trades if item.get("realized_exchange_pnl") is not None]
        wins = [value for value in pnl if value > 0]
        losses = [value for value in pnl if value < 0]
        fees = sum((_d(item.get("exchange_fees")) for item in trades), Decimal("0"))
        gross = sum(pnl, Decimal("0")) + fees
        net = sum(pnl, Decimal("0"))
        profit_factor = (
            sum(wins, Decimal("0")) / abs(sum(losses, Decimal("0")))
            if losses else None
        )
        holdings = []
        for item in trades:
            if item.get("created_at") and item.get("updated_at"):
                holdings.append((datetime.fromisoformat(item["updated_at"]) - datetime.fromisoformat(item["created_at"])).total_seconds())
        by_strategy = Counter(str(item.get("strategy_name") or "unknown") for item in signals)
        by_symbol = Counter(str(item.get("symbol") or "unknown") for item in signals)
        order_strategy = Counter(str(item.get("strategy_name") or "unknown") for item in executions)
        order_symbol = Counter(str(item.get("symbol") or "unknown") for item in executions)
        exit_reasons = Counter(str(item.get("close_reason") or "unknown") for item in trades)
        return {
            "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(),
            "signals_by_strategy": dict(by_strategy), "signals_by_symbol": dict(by_symbol),
            "rejections_by_reason": dict(Counter(str(item.get("rejection_reason") or item.get("reason") or "unknown") for item in rejections)),
            "orders_by_strategy": dict(order_strategy), "orders_by_symbol": dict(order_symbol),
            "completed_trades": len(trades),
            "open_positions": sum(str(item.get("state")) == "DEMO_POSITION_OPEN" for item in executions),
            "long_short_split": dict(Counter(str(item.get("side") or "unknown") for item in executions)),
            "win_rate": str(Decimal(len(wins)) / Decimal(len(pnl))) if pnl else None,
            "average_win": str(sum(wins, Decimal("0")) / len(wins)) if wins else None,
            "average_loss": str(sum(losses, Decimal("0")) / len(losses)) if losses else None,
            "expectancy_after_fees": str(net / len(pnl)) if pnl else None,
            "profit_factor": str(profit_factor) if profit_factor is not None else None,
            "gross_pnl": str(gross), "total_fees": str(fees), "net_pnl": str(net),
            "maximum_drawdown": str(_max_drawdown(pnl)),
            "maximum_concurrent_positions": _maximum_concurrency(executions),
            "maximum_concurrent_notional": str(_maximum_notional(executions)),
            "average_holding_seconds": sum(holdings) / len(holdings) if holdings else None,
            "exit_counts": dict(exit_reasons),
            "reconciliation_incidents": sum("RECONCIL" in str(item.get("event_type") or "") for item in incidents),
            "websocket_reconnects": sum(int((item.get("payload") or {}).get("reconnects") or 0) for item in incidents if item.get("event_type") == "WEBSOCKET_RECONNECT"),
            "stale_data_incidents": sum(item.get("event_type") == "STALE_DATA" for item in incidents),
            "top_rejection_reasons": Counter(str(item.get("rejection_reason") or item.get("reason") or "unknown") for item in rejections).most_common(10),
        }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["run_id"])
        writer.writeheader()
        writer.writerows(rows)


def _signal_row(item: dict[str, Any]) -> dict[str, Any]:
    scores = item.get("score_components") or {}
    return {
        "run_id": item.get("run_id"), "candidate_id": item.get("id"),
        "created_at": item.get("created_at"), "strategy": item.get("strategy_name"),
        "strategy_version": item.get("strategy_version"), "symbol": item.get("symbol"),
        "side": item.get("side"), "market_regime": item.get("market_regime"),
        "raw_score": item.get("raw_strategy_score"), "final_score": scores.get("final_score"),
        "threshold": item.get("threshold"), "distance_to_threshold": item.get("distance_to_threshold"),
        "estimated_edge_bps": item.get("estimated_edge_bps"),
        "entry_reason": item.get("entry_reason"), "state": item.get("state"),
    }


def _rejection_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": item.get("run_id"), "candidate_id": item.get("id") or item.get("candidate_id"),
        "created_at": item.get("created_at"), "strategy": item.get("strategy_name"),
        "symbol": item.get("symbol"),
        "reason": item.get("rejection_reason") or item.get("reason"),
    }


def _trade_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": item.get("run_id"), "execution_id": item.get("id"),
        "candidate_id": item.get("candidate_id"), "strategy": item.get("strategy_name"),
        "symbol": item.get("symbol"), "side": item.get("side"),
        "leverage": item.get("leverage"), "quantity": item.get("accepted_quantity"),
        "entry_price": item.get("average_fill_price"), "exit_price": item.get("average_close_price"),
        "entry_slippage": item.get("entry_slippage"), "exit_slippage": item.get("exit_slippage"),
        "fees": item.get("exchange_fees"), "tp": item.get("take_profit"),
        "sl": item.get("stop_loss"), "mfe": item.get("maximum_favorable_excursion"),
        "mae": item.get("maximum_adverse_excursion"), "exit_reason": item.get("close_reason"),
        "net_pnl": item.get("realized_exchange_pnl"), "opened_at": item.get("created_at"),
        "closed_at": item.get("closed_at") or item.get("updated_at"),
        "signal_to_order_latency_ms": _latency_ms(item.get("signal_created_at"), item.get("order_submitted_at")),
        "order_to_fill_latency_ms": _latency_ms(item.get("order_submitted_at"), item.get("first_fill_at")),
    }


def _d(value: object) -> Decimal:
    return Decimal(str(value or "0"))


def _max_drawdown(pnl: list[Decimal]) -> Decimal:
    equity = peak = Decimal("0"); maximum = Decimal("0")
    for value in pnl:
        equity += value; peak = max(peak, equity); maximum = max(maximum, peak - equity)
    return maximum


def _maximum_concurrency(executions: list[dict[str, Any]]) -> int:
    events: list[tuple[datetime, int]] = []
    for item in executions:
        if item.get("created_at"):
            events.append((datetime.fromisoformat(item["created_at"]), 1))
        if item.get("updated_at") and str(item.get("state")) in TERMINAL_STATES:
            events.append((datetime.fromisoformat(item["updated_at"]), -1))
    current = maximum = 0
    for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
        current += delta; maximum = max(maximum, current)
    return maximum


def _maximum_notional(executions: list[dict[str, Any]]) -> Decimal:
    return max((_d(item.get("accepted_quantity")) * _d(item.get("average_fill_price")) for item in executions), default=Decimal("0"))


def _latency_ms(start: object, finish: object) -> float | None:
    if not start or not finish:
        return None
    return (datetime.fromisoformat(str(finish)) - datetime.fromisoformat(str(start))).total_seconds() * 1000
