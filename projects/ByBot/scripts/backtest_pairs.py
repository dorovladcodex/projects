"""Walk-forward backtest of pair mean reversion on perpetuals.

The last untested family in the operator's list. It trades a spread rather
than a direction, so it needs no speed advantage, and it holds for days, which
is the only way a 13 bps round trip amortises.

Offline and read-only. No exchange call, no order, no Demo path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.costs import STRESS_MULTIPLIERS, CostModel, Liquidity, stressed  # noqa: E402
from app.backtest.data import load_dataset  # noqa: E402
from app.backtest.metrics import evaluate_portfolio, promotion_gates  # noqa: E402
from app.backtest.pairs import PairsParameters, PairsStrategy  # noqa: E402
from app.backtest.portfolio import PortfolioEngine  # noqa: E402
from app.backtest.validation import run_walk_forward  # noqa: E402
from scripts.history_backfill import DEFAULT_SYMBOLS, guard_research_database  # noqa: E402


def parse_day(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--from", dest="start", default="2021-01-01")
    parser.add_argument("--to", dest="end", default="2026-08-12")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--lookback-hours", type=int, default=24 * 21)
    parser.add_argument("--entry-z", type=float, default=2.0)
    parser.add_argument("--exit-z", type=float, default=0.5)
    parser.add_argument("--max-pairs", type=int, default=3)
    parser.add_argument("--notional", type=float, default=1_000.0)
    parser.add_argument("--rebalance-hours", type=int, default=24)
    parser.add_argument("--max-holding-hours", type=int, default=24 * 30)
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    parser.add_argument("--maker", action="store_true")
    parser.add_argument("--json-out", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.database_url:
        raise SystemExit("--database-url or DATABASE_URL is required")
    dsn = guard_research_database(args.database_url)

    start_ms, end_ms = parse_day(args.start), parse_day(args.end)
    if start_ms >= end_ms:
        raise SystemExit("--from must be earlier than --to")

    print(f"loading {len(args.symbols)} symbols {args.start}..{args.end}", flush=True)
    dataset = load_dataset(dsn, args.symbols, from_ms=start_ms, to_ms=end_ms, with_spot=False)
    if not dataset.timeline:
        raise SystemExit("no data in range; run history_backfill.py first")

    pair_count = len(dataset.symbols) * (len(dataset.symbols) - 1) // 2
    print(
        f"{len(dataset.timeline):,} hourly bars, {len(dataset.symbols)} symbols, "
        f"{pair_count} candidate pairs", flush=True
    )

    parameters = PairsParameters(
        lookback_hours=args.lookback_hours,
        entry_z=args.entry_z,
        exit_z=args.exit_z,
        max_pairs=args.max_pairs,
        notional_per_leg=args.notional,
        rebalance_hours=args.rebalance_hours,
        max_holding_hours=args.max_holding_hours,
    )
    liquidity = Liquidity.MAKER if args.maker else Liquidity.TAKER
    base_costs = CostModel()
    print(
        f"entry |z|>{args.entry_z}, exit |z|<{args.exit_z}, "
        f"lookback {args.lookback_hours}h, max {args.max_pairs} pairs, "
        f"{base_costs.round_trip_bps('perp', liquidity)} bps per leg round trip "
        f"({liquidity.value})\n", flush=True
    )

    def make_report(costs: CostModel):
        return run_walk_forward(
            dataset,
            lambda: PairsStrategy(parameters),
            costs,
            fold_count=args.folds,
            holdout_fraction=args.holdout_fraction,
            starting_equity=args.equity,
            liquidity=liquidity,
            engine_factory=lambda: PortfolioEngine(
                dataset, costs, starting_equity=args.equity, liquidity=liquidity
            ),
            evaluator=evaluate_portfolio,
        )

    report = make_report(base_costs)
    print("walk-forward folds (out of sample):")
    for definition, metrics in zip(report.fold_definitions, report.folds):
        span = (definition.test_to_ms - definition.test_from_ms) / 86_400_000
        print(f"  fold {definition.index} ({span:5.0f}d): {metrics.summary()}")
    if report.holdout is not None:
        print(f"\nfrozen holdout: {report.holdout.summary()}")
        print(f"  median holding: {report.holdout.median_holding_hours:.0f}h")

    print("\ncost stress:")
    stress_positive: dict[str, int] = {}
    for multiplier in STRESS_MULTIPLIERS:
        stress = make_report(stressed(base_costs, multiplier))
        aggregate = sum(fold.net_pnl for fold in stress.folds)
        stress_positive[str(multiplier)] = stress.positive_folds
        print(
            f"  x{multiplier}: net {aggregate:+9.2f}  "
            f"positive folds {stress.positive_folds}/{len(stress.folds)}"
        )

    gates = promotion_gates(report.folds, report.holdout, stress_positive_folds=stress_positive)
    print("\npromotion gates:")
    for gate in gates:
        print(f"  {'PASS' if gate.passed else 'FAIL'}  {gate.name:32s} {gate.detail}")

    passed = all(gate.passed for gate in gates)
    print(f"\nRESULT [pairs]: {'PROMOTABLE' if passed else 'NOT SUPPORTED'}")
    print("No Demo run is implied by this result; it is offline evidence only.")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "parameters": parameters.__dict__,
                    "liquidity": liquidity.value,
                    "folds": [metrics.__dict__ for metrics in report.folds],
                    "holdout": report.holdout.__dict__ if report.holdout else None,
                    "gates": [gate.__dict__ for gate in gates],
                    "promotable": passed,
                },
                indent=2, default=str,
            )
        )
        print(f"wrote {args.json_out}")

    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
