"""Parameter selection on training folds only, verified once on the frozen holdout.

This measures a *procedure*, not a configuration. For each chronological fold
it searches the grid on that fold's training window, then reports what the
chosen configuration did on the untouched test window. The holdout is scored
once at the end with whichever configuration the procedure picked most often.

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

from app.backtest.costs import CostModel, Liquidity  # noqa: E402
from app.backtest.data import load_dataset  # noqa: E402
from app.backtest.metrics import promotion_gates  # noqa: E402
from app.backtest.search import Grid, run_search  # noqa: E402
from app.backtest.signals import (  # noqa: E402
    CrossSectionalParameters,
    FundingTiltStrategy,
    MomentumStrategy,
)
from scripts.history_backfill import DEFAULT_SYMBOLS, guard_research_database  # noqa: E402

STRATEGIES = {"funding_tilt": FundingTiltStrategy, "momentum": MomentumStrategy}


def parse_day(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--strategy", nargs="+", default=list(STRATEGIES), choices=list(STRATEGIES))
    parser.add_argument("--from", dest="start", default="2021-01-01")
    parser.add_argument("--to", dest="end", default="2026-08-12")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--gross-notional", type=float, default=4_000.0)
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

    liquidity = Liquidity.MAKER if args.maker else Liquidity.TAKER
    costs = CostModel()
    grid = Grid()
    base = CrossSectionalParameters(gross_notional=args.gross_notional)

    payload: dict[str, object] = {}
    exit_code = 2

    for name in args.strategy:
        print(f"\n{'=' * 66}\n{name} ({liquidity.value})\n{'=' * 66}", flush=True)
        report = run_search(
            dataset,
            STRATEGIES[name],
            base,
            costs,
            grid=grid,
            fold_count=args.folds,
            holdout_fraction=args.holdout_fraction,
            starting_equity=args.equity,
            liquidity=liquidity,
        )
        print(f"grid: {report.grid_size} configurations searched per fold\n")

        for choice in report.choices:
            chosen = choice.chosen
            print(
                f"  fold {choice.fold.index}: chose lookback={chosen.lookback_hours}h "
                f"rebalance={chosen.rebalance_hours}h basket={chosen.basket_size}"
            )
            print(f"    train (used for selection): {choice.train.summary()}")
            print(f"    test  (out of sample):      {choice.test.summary()}")

        print(
            f"\nselection stability: {report.selection_stability * 100:.0f}% "
            f"of folds agreed on one configuration"
        )
        print(f"aggregate out-of-sample: {report.aggregate_test_pnl:+.2f}")

        if report.holdout is not None and report.holdout_parameters is not None:
            chosen = report.holdout_parameters
            print(
                f"\nfrozen holdout with lookback={chosen.lookback_hours}h "
                f"rebalance={chosen.rebalance_hours}h basket={chosen.basket_size}:"
            )
            print(f"  {report.holdout.summary()}")

        gates = promotion_gates(report.test_metrics, report.holdout)
        print("\npromotion gates:")
        for gate in gates:
            print(f"  {'PASS' if gate.passed else 'FAIL'}  {gate.name:32s} {gate.detail}")

        passed = all(gate.passed for gate in gates)
        print(f"\nRESULT [{name}]: {'PROMOTABLE' if passed else 'NOT SUPPORTED'}")
        if passed:
            exit_code = 0

        payload[name] = {
            "grid_size": report.grid_size,
            "selection_stability": report.selection_stability,
            "choices": [
                {
                    "fold": choice.fold.index,
                    "chosen": choice.chosen.__dict__,
                    "train": choice.train.__dict__,
                    "test": choice.test.__dict__,
                }
                for choice in report.choices
            ],
            "holdout": report.holdout.__dict__ if report.holdout else None,
            "holdout_parameters": (
                report.holdout_parameters.__dict__ if report.holdout_parameters else None
            ),
            "gates": [gate.__dict__ for gate in gates],
            "promotable": passed,
        }

    print("\nNo Demo run is implied by any result here; this is offline evidence only.")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str))
        print(f"wrote {args.json_out}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
