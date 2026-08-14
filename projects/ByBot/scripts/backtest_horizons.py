"""Sweep decision horizons on the 1m clock, taker against maker.

This is the test the minute data was loaded for. Two facts drive it:

  * An hourly bar's range already contains every minute inside it, so a
    resting order exposed for an hour is reached almost always. Only a short
    exposure makes a maker fill rate mean anything.
  * Every hypothesis so far was rebalanced daily, while the production bot
    traded on a 4.6 minute median. Nothing between those was ever tested.

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
from app.backtest.metrics import evaluate_portfolio, promotion_gates  # noqa: E402
from app.backtest.portfolio import PortfolioEngine  # noqa: E402
from app.backtest.signals import (  # noqa: E402
    CrossSectionalParameters,
    FundingTiltStrategy,
    MomentumStrategy,
    ReversionStrategy,
)
from app.backtest.validation import run_walk_forward  # noqa: E402
from scripts.history_backfill import DEFAULT_SYMBOLS, guard_research_database  # noqa: E402

STRATEGIES = {
    "momentum": MomentumStrategy,
    "reversion": ReversionStrategy,
    "funding_tilt": FundingTiltStrategy,
}

# Rebalance cadence in minutes, spanning the untested gap between the
# production bot's 4.6 minute median and the daily books already rejected.
DEFAULT_HORIZONS = (15, 60, 240, 720)


def parse_day(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--strategy", default="momentum", choices=list(STRATEGIES))
    parser.add_argument("--from", dest="start", default="2025-01-01")
    parser.add_argument("--to", dest="end", default="2026-08-12")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--horizons", nargs="+", type=int, default=list(DEFAULT_HORIZONS))
    parser.add_argument("--lookback-multiple", type=int, default=8,
                        help="signal lookback as a multiple of the rebalance cadence")
    parser.add_argument("--basket-size", type=int, default=3)
    parser.add_argument("--gross-notional", type=float, default=4_000.0)
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument("--folds", type=int, default=4)
    parser.add_argument("--holdout-fraction", type=float, default=0.2)
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

    print(f"loading 1m bars: {len(args.symbols)} symbols {args.start}..{args.end}", flush=True)
    dataset = load_dataset(
        dsn, args.symbols, from_ms=start_ms, to_ms=end_ms, with_spot=False, interval="1"
    )
    if not dataset.timeline:
        raise SystemExit("no 1m data in range; run history_backfill.py --interval 1")
    print(f"{len(dataset.timeline):,} minute bars on the clock, "
          f"{len(dataset.symbols)} symbols\n", flush=True)

    strategy_class = STRATEGIES[args.strategy]
    costs = CostModel()
    results: dict[str, object] = {}
    promotable: list[str] = []

    for minutes in args.horizons:
        lookback_hours = max(1, minutes * args.lookback_multiple // 60)
        print(f"{'=' * 72}")
        print(f"rebalance every {minutes}m, signal lookback {lookback_hours}h")
        print(f"{'=' * 72}", flush=True)

        parameters = CrossSectionalParameters(
            lookback_hours=lookback_hours,
            rebalance_minutes=minutes,
            basket_size=args.basket_size,
            gross_notional=args.gross_notional,
        )

        for liquidity in (Liquidity.TAKER, Liquidity.MAKER):
            report = run_walk_forward(
                dataset,
                lambda: strategy_class(parameters),
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
            aggregate = sum(fold.net_pnl for fold in report.folds)
            gates = promotion_gates(report.folds, report.holdout)
            passed = all(gate.passed for gate in gates)
            holdout = report.holdout

            line = (
                f"  {liquidity.value:5s} fill={report.fill_rate * 100:5.1f}%  "
                f"folds {report.positive_folds}/{len(report.folds)} "
                f"agg {aggregate:+9.2f}"
            )
            if holdout is not None:
                line += (
                    f"  holdout {holdout.net_pnl:+9.2f} "
                    f"({holdout.expectancy_bps:+7.2f} bps, {holdout.trades} trades)"
                )
            print(line, flush=True)
            failed = [gate.name for gate in gates if not gate.passed]
            print(f"        {'PROMOTABLE' if passed else 'failed: ' + ', '.join(failed)}")

            key = f"{minutes}m_{liquidity.value}"
            results[key] = {
                "rebalance_minutes": minutes,
                "lookback_hours": lookback_hours,
                "liquidity": liquidity.value,
                "fill_rate": report.fill_rate,
                "positive_folds": report.positive_folds,
                "aggregate": aggregate,
                "holdout": holdout.__dict__ if holdout else None,
                "gates": [gate.__dict__ for gate in gates],
                "promotable": passed,
            }
            if passed:
                promotable.append(key)

    print(f"\n{'=' * 72}")
    print(f"RESULT: {', '.join(promotable) if promotable else 'NO HORIZON SUPPORTED'}")
    print("No Demo run is implied by any result here; this is offline evidence only.")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(results, indent=2, default=str))
        print(f"wrote {args.json_out}")

    return 0 if promotable else 2


if __name__ == "__main__":
    raise SystemExit(main())
