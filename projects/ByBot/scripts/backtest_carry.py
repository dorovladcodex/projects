"""Walk-forward backtest of the funding-carry hypothesis.

Offline and read-only: it reads the research database, touches no exchange and
submits no orders. Nothing here can enable Demo or live execution.

    python scripts/backtest_carry.py --from 2021-01-01 --to 2026-08-12 \
        --database-url postgresql://.../bybot_claude
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Measured across 66,199 settlements in history.funding_rate, 2020-2026.
MEASURED_FUNDING_BPS_PER_DAY = Decimal("3.326")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.costs import STRESS_MULTIPLIERS, CostModel, Liquidity, stressed  # noqa: E402
from app.backtest.data import coverage_report, load_dataset  # noqa: E402
from app.backtest.metrics import promotion_gates  # noqa: E402
from app.backtest.strategies import CarryParameters, FundingCarryStrategy  # noqa: E402
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
    parser.add_argument("--lookback-hours", type=int, default=24 * 14)
    parser.add_argument("--entry-bps-per-day", type=float, default=3.0)
    parser.add_argument("--exit-bps-per-day", type=float, default=0.5)
    parser.add_argument("--max-positions", type=int, default=4)
    parser.add_argument("--notional", type=float, default=1_000.0)
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
    parser.add_argument("--maker", action="store_true", help="assume maker fills on both legs")
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
    dataset = load_dataset(dsn, args.symbols, from_ms=start_ms, to_ms=end_ms)
    if not dataset.timeline:
        raise SystemExit("no data in the requested range; run history_backfill.py first")

    print(f"\ncoverage (spot leg is the binding constraint):")
    for row in coverage_report(dataset):
        print(
            f"  {row.symbol:10s} perp={row.perp_bars:>6,} spot={row.spot_bars:>6,} "
            f"funding={row.funding_events:>5,} both_legs={row.both_legs:>6,} "
            f"({row.spot_coverage_pct:5.1f}%)"
        )

    parameters = CarryParameters(
        lookback_hours=args.lookback_hours,
        entry_bps_per_day=args.entry_bps_per_day,
        exit_bps_per_day=args.exit_bps_per_day,
        max_positions=args.max_positions,
        notional_per_leg=args.notional,
    )
    liquidity = Liquidity.MAKER if args.maker else Liquidity.TAKER
    base_costs = CostModel()

    print(
        f"\ncost model: carry round trip = "
        f"{base_costs.carry_round_trip_bps(liquidity)} bps ({liquidity.value})"
    )
    breakeven = base_costs.breakeven_days(MEASURED_FUNDING_BPS_PER_DAY, liquidity)
    print(
        f"  at the measured {MEASURED_FUNDING_BPS_PER_DAY} bps/day funding average, "
        f"breakeven ~{breakeven:.1f} days"
    )

    report = run_walk_forward(
        dataset,
        lambda: FundingCarryStrategy(parameters),
        base_costs,
        fold_count=args.folds,
        holdout_fraction=args.holdout_fraction,
        starting_equity=args.equity,
        liquidity=liquidity,
    )

    print("\nwalk-forward folds (out of sample):")
    for definition, metrics in zip(report.fold_definitions, report.folds):
        span = (definition.test_to_ms - definition.test_from_ms) / 86_400_000
        print(f"  fold {definition.index} ({span:5.0f}d): {metrics.summary()}")

    if report.holdout is not None:
        print(f"\nfrozen holdout: {report.holdout.summary()}")

    print("\ncost stress (taker, multiplied):")
    for multiplier in STRESS_MULTIPLIERS:
        stress = run_walk_forward(
            dataset,
            lambda: FundingCarryStrategy(parameters),
            stressed(base_costs, multiplier),
            fold_count=args.folds,
            holdout_fraction=args.holdout_fraction,
            starting_equity=args.equity,
            liquidity=liquidity,
        )
        aggregate = sum(fold.net_pnl for fold in stress.folds)
        print(
            f"  x{multiplier}: net {aggregate:+9.2f}  "
            f"positive folds {stress.positive_folds}/{len(stress.folds)}"
        )

    print("\npromotion gates:")
    gates = promotion_gates(report.folds, report.holdout)
    for gate in gates:
        print(f"  {'PASS' if gate.passed else 'FAIL'}  {gate.name:32s} {gate.detail}")

    passed = all(gate.passed for gate in gates)
    print(f"\nRESULT: {'PROMOTABLE' if passed else 'NOT SUPPORTED'}")
    print("No Demo run is implied by this result; it is offline evidence only.")

    if args.json_out:
        payload = {
            "parameters": parameters.__dict__,
            "liquidity": liquidity.value,
            "folds": [metrics.__dict__ for metrics in report.folds],
            "holdout": report.holdout.__dict__ if report.holdout else None,
            "gates": [gate.__dict__ for gate in gates],
            "promotable": passed,
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str))
        print(f"wrote {args.json_out}")

    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
