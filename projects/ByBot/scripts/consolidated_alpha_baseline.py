from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import statistics
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import DemoDiagnosticsConfig  # noqa: E402
from app.db.persistence import PersistenceRepository  # noqa: E402
from app.bybit.demo import TERMINAL_DEMO_STATES  # noqa: E402


ZERO = Decimal("0")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a read-only multi-run V2 alpha baseline."
    )
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = DemoDiagnosticsConfig.load(env_path=ROOT / ".env")
    repo = PersistenceRepository(config.database_url, create_schema=False)
    if not repo.available:
        raise SystemExit("database persistence is unavailable")
    run_ids = list(dict.fromkeys(args.run_id))
    candidates = {
        str(candidate.id): candidate
        for run_id in run_ids
        for candidate in repo.load_v2_signal_candidates(run_id)
    }
    records = [
        record
        for record in repo.load_demo_executions()
        if record.run_id in run_ids
        and record.state in TERMINAL_DEMO_STATES
        and record.accounting_status == "FINAL"
        and record.closed_at is not None
    ]
    rows = [
        build_row(record, candidates.get(str(record.candidate_id)), records)
        for record in records
    ]
    rows.sort(key=lambda row: row["closed_at"])
    summary = summarize(rows)
    summary["run_ids"] = run_ids
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    summary["read_only"] = True
    summary["recommendations"] = recommendations(summary)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "consolidated-alpha-baseline.json"
    md_path = args.output_dir / "consolidated-alpha-baseline.md"
    json_path.write_text(
        json.dumps({"summary": summary, "trades": rows}, indent=2, default=str),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps({
        "runs": run_ids,
        "trades": summary["total_trades"],
        "net_pnl": summary["net_pnl"],
        "profit_factor": summary["profit_factor"],
        "json": str(json_path),
        "markdown": str(md_path),
        "read_only": True,
    }, indent=2))
    return 0


def build_row(record: Any, candidate: Any, all_records: list[Any]) -> dict[str, Any]:
    sizing = dict(record.sizing_details or {})
    opened = record.first_fill_at or record.exchange_fill_at or record.created_at
    closed = record.closed_at
    gross = dec(record.gross_realized_pnl)
    entry_fee = dec(record.entry_fees)
    close_fee = dec(record.close_fees)
    funding = dec(record.funding_pnl)
    net = dec(record.realized_exchange_pnl)
    risk = dec(sizing.get("risk_budget_usdt"))
    simultaneous = sum(
        1
        for other in all_records
        if (
            (other.first_fill_at or other.exchange_fill_at or other.created_at)
            <= opened
            and (other.closed_at is None or other.closed_at >= opened)
        )
    )
    expected_net_bps = dec(sizing.get("expected_net_edge_bps"))
    notional = dec(
        sizing.get("normalized_accepted_notional_usdt")
        or (
            record.accepted_quantity * record.average_fill_price
            if record.average_fill_price is not None else ZERO
        )
    )
    expected_net_usdt = notional * expected_net_bps / Decimal("10000")
    score = dec(sizing.get("final_score"))
    regime = (
        candidate.feature_snapshot.market_regime
        if candidate is not None else "UNKNOWN"
    )
    return {
        "run_id": record.run_id,
        "execution_id": str(record.id),
        "symbol": record.symbol.value,
        "strategy": record.strategy_name or "UNKNOWN",
        "side": record.side.value,
        "exit_reason": record.exit_attribution or record.close_reason or "UNKNOWN",
        "confidence_tier": sizing.get("confidence_tier") or "UNKNOWN",
        "final_score": str(score),
        "final_score_bucket": bucket(score, Decimal("0.05")),
        "expected_net_edge_bps": str(expected_net_bps),
        "expected_net_edge_bucket": bucket(expected_net_bps, Decimal("5")),
        "market_regime": regime,
        "hour_of_day_utc": opened.hour,
        "simultaneous_position_count": simultaneous,
        "opened_at": opened.isoformat(),
        "closed_at": closed.isoformat(),
        "entry_price": str(record.average_fill_price),
        "exit_price": str(record.average_close_price),
        "notional": str(notional),
        "planned_risk": str(risk),
        "gross_pnl": str(gross),
        "entry_fee": str(entry_fee),
        "exit_fee": str(close_fee),
        "fees": str(entry_fee + close_fee),
        "funding": str(funding),
        "net_pnl": str(net),
        "r_multiple": str(net / risk) if risk > 0 else None,
        "expected_net_usdt": str(expected_net_usdt),
        "underperformed_expected_edge": bool(net < expected_net_usdt),
        "gross_did_not_cover_fees": bool(gross <= entry_fee + close_fee),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl = [dec(row["net_pnl"]) for row in rows]
    gross = [dec(row["gross_pnl"]) for row in rows]
    fees = [dec(row["fees"]) for row in rows]
    funding = [dec(row["funding"]) for row in rows]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    positive_gross = sum((value for value in gross if value > 0), ZERO)
    equity = ZERO
    peak = ZERO
    maximum_drawdown = ZERO
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, peak - equity)
    r_values = sorted(
        dec(row["r_multiple"])
        for row in rows
        if row.get("r_multiple") is not None
    )
    return {
        "total_trades": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": str(Decimal(len(wins)) / Decimal(len(rows))) if rows else None,
        "gross_pnl": str(sum(gross, ZERO)),
        "fees": str(sum(fees, ZERO)),
        "funding": str(sum(funding, ZERO)),
        "net_pnl": str(sum(pnl, ZERO)),
        "profit_factor": (
            str(sum(wins, ZERO) / abs(sum(losses, ZERO)))
            if losses else None
        ),
        "expectancy": str(sum(pnl, ZERO) / Decimal(len(rows))) if rows else None,
        "average_gross_edge_per_trade": (
            str(sum(gross, ZERO) / Decimal(len(rows))) if rows else None
        ),
        "average_fee_per_trade": (
            str(sum(fees, ZERO) / Decimal(len(rows))) if rows else None
        ),
        "median_r": str(median_decimal(r_values)) if r_values else None,
        "r_distribution": {
            "minimum": str(min(r_values)) if r_values else None,
            "p25": str(percentile(r_values, Decimal("0.25"))) if r_values else None,
            "median": str(median_decimal(r_values)) if r_values else None,
            "p75": str(percentile(r_values, Decimal("0.75"))) if r_values else None,
            "maximum": str(max(r_values)) if r_values else None,
        },
        "maximum_drawdown": str(maximum_drawdown),
        "maximum_win_streak": streak(pnl, positive=True),
        "maximum_loss_streak": streak(pnl, positive=False),
        "fees_pct_of_positive_gross_pnl": (
            str(sum(fees, ZERO) / positive_gross * Decimal("100"))
            if positive_gross > 0 else None
        ),
        "gross_did_not_cover_fees_count": sum(
            bool(row["gross_did_not_cover_fees"]) for row in rows
        ),
        "underperformed_expected_edge_count": sum(
            bool(row["underperformed_expected_edge"]) for row in rows
        ),
        "by_strategy": grouped(rows, "strategy"),
        "by_side": grouped(rows, "side"),
        "by_symbol": grouped(rows, "symbol"),
        "by_exit_reason": grouped(rows, "exit_reason"),
        "by_confidence_tier": grouped(rows, "confidence_tier"),
        "by_final_score_bucket": grouped(rows, "final_score_bucket"),
        "by_expected_net_edge_bucket": grouped(
            rows, "expected_net_edge_bucket"
        ),
        "by_market_regime": grouped(rows, "market_regime"),
        "by_hour_of_day_utc": grouped(rows, "hour_of_day_utc"),
        "by_simultaneous_position_count": grouped(
            rows, "simultaneous_position_count"
        ),
    }


def grouped(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "UNKNOWN")].append(row)
    result: dict[str, Any] = {}
    for key, items in sorted(groups.items()):
        pnl = [dec(item["net_pnl"]) for item in items]
        gross = sum((dec(item["gross_pnl"]) for item in items), ZERO)
        fee = sum((dec(item["fees"]) for item in items), ZERO)
        result[key] = {
            "trades": len(items),
            "wins": sum(value > 0 for value in pnl),
            "net_pnl": str(sum(pnl, ZERO)),
            "gross_pnl": str(gross),
            "fees": str(fee),
            "expectancy": str(sum(pnl, ZERO) / Decimal(len(items))),
        }
    return result


def recommendations(summary: dict[str, Any]) -> list[dict[str, str]]:
    recommendations: list[dict[str, str]] = []
    for name in ("LiquidationMomentumStrategy", "OIFundingSqueezeStrategy"):
        item = summary["by_strategy"].get(name)
        if item and item["trades"] >= 20 and dec(item["net_pnl"]) < 0:
            classification = "enough evidence for immediate shadow restriction"
        else:
            classification = "requires more data"
        recommendations.append({
            "scope": name,
            "classification": classification,
            "recommendation": (
                "Continue collecting frozen evidence; shadow-restrict only if "
                "the negative expectancy remains stable after cost attribution."
            ),
        })
    if dec(summary["fees"]) > max(dec(summary["gross_pnl"]), ZERO):
        recommendations.append({
            "scope": "portfolio",
            "classification": "likely fee/economics issue",
            "recommendation": (
                "Evaluate maker-capable entry/exit execution and trade-level "
                "gross-edge coverage in shadow mode before changing gates."
            ),
        })
    for reason in ("stale_signal", "maximum_holding_time"):
        item = summary["by_exit_reason"].get(reason)
        if item and dec(item["net_pnl"]) < 0:
            recommendations.append({
                "scope": reason,
                "classification": "likely exit-timing issue",
                "recommendation": (
                    "Run a shadow counterfactual exit study; do not alter the "
                    "production exit rule from this baseline alone."
                ),
            })
    recommendations.append({
        "scope": "direction",
        "classification": "likely direction/regime issue"
        if (
            summary["by_side"].get("BUY")
            and summary["by_side"].get("SELL")
            and (
                dec(summary["by_side"]["BUY"]["net_pnl"])
                * dec(summary["by_side"]["SELL"]["net_pnl"])
            ) < 0
        ) else "requires more data",
        "recommendation": (
            "Compare BUY/SELL expectancy by regime in shadow mode; preserve "
            "current production direction logic until evidence is sufficient."
        ),
    })
    return recommendations


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Consolidated V2 alpha baseline",
        "",
        f"- Runs: {', '.join(summary['run_ids'])}",
        f"- Trades: {summary['total_trades']}",
        f"- Wins / losses: {summary['wins']} / {summary['losses']}",
        f"- Win rate: {summary['win_rate']}",
        f"- Gross PnL: {summary['gross_pnl']}",
        f"- Fees: {summary['fees']}",
        f"- Funding: {summary['funding']}",
        f"- Net PnL: {summary['net_pnl']}",
        f"- Profit factor: {summary['profit_factor']}",
        f"- Expectancy: {summary['expectancy']}",
        f"- Maximum drawdown: {summary['maximum_drawdown']}",
        "",
        "## Strategy breakdown",
        "",
    ]
    for key, value in summary["by_strategy"].items():
        lines.append(
            f"- {key}: {value['trades']} trades, net {value['net_pnl']}, "
            f"expectancy {value['expectancy']}"
        )
    lines += ["", "## Recommendations", ""]
    for item in summary["recommendations"]:
        lines.append(
            f"- **{item['scope']} — {item['classification']}**: "
            f"{item['recommendation']}"
        )
    lines += [
        "",
        "> This is a descriptive baseline, not evidence of profitability.",
        "",
    ]
    return "\n".join(lines)


def dec(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def bucket(value: Decimal, width: Decimal) -> str:
    low = (value // width) * width
    return f"{low}_{low + width}"


def median_decimal(values: list[Decimal]) -> Decimal:
    return Decimal(str(statistics.median(values)))


def percentile(values: list[Decimal], fraction: Decimal) -> Decimal:
    if len(values) == 1:
        return values[0]
    position = fraction * Decimal(len(values) - 1)
    low = int(position)
    high = min(low + 1, len(values) - 1)
    weight = position - Decimal(low)
    return values[low] * (Decimal("1") - weight) + values[high] * weight


def streak(values: list[Decimal], *, positive: bool) -> int:
    best = current = 0
    for value in values:
        matches = value > 0 if positive else value < 0
        current = current + 1 if matches else 0
        best = max(best, current)
    return best


if __name__ == "__main__":
    raise SystemExit(main())
