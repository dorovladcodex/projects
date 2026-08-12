"""Walk-forward backtest of the two perpetual-only cross-sectional hypotheses.

Both books are dollar-neutral long/short on perpetuals, so neither pays the
20 bps spot round trip that made cash-and-carry unviable.

  funding_tilt  long the cheapest funding, short the most expensive
  momentum      long recent relative winners, short recent losers

Offline and read-only: no exchange call, no order, no Demo path.

    python scripts/backtest_crosssectional.py --strategy funding_tilt momentum \
        --from 2021-01-01 --to 2026-08-12 --database-url postgresql://.../bybot_claude
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
from app.backtest.portfolio import PortfolioEngine  # noqa: E402
from app.backtest.signals import (  # noqa: E402
    CrossSectionalParameters,
    FundingTiltStrategy,
    MomentumStrategy,
)
from app.backtest.validation import run_walk_forward  # noqa: E402
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
    parser.add_argument("--lookback-hours", type=int, default=24 * 7)
    parser.add_argument("--rebalance-hours", type=int, default=24)
    parser.add_argument("--basket-size", type=int, default=3)
    parser.add_argument("--gross-notional", type=float, default=4_000.0)
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

    parameters = CrossSectionalParameters(
        lookback_hours=args.lookback_hours,
        rebalance_hours=args.rebalance_hours,
        basket_size=args.basket_size,
        gross_notional=args.gross_notional,
    )
    liquidity = Liquidity.MAKER if args.maker else Liquidity.TAKER
    base_costs = CostModel()
    pair_round_trip = base_costs.round_trip_bps("perp", liquidity)
    print(
        f"perpetual-only book: {pair_round_trip} bps round trip per leg "
        f"({liquidity.value}), no spot leg\n"
        f"rebalance every {args.rebalance_hours}h, "
        f"{args.basket_size} long / {args.basket_size} short"
    )

    payload: dict[str, object] = {}
    exit_code = 2

    for name in args.strategy:
        strategy_class = STRATEGIES[name]
        print(f"\n{'=' * 62}\n{name}\n{'=' * 62}")

        def make_report(costs: CostModel):
            return run_walk_forward(
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

        report = make_report(base_costs)
        print("walk-forward folds (out of sample):")
        for definition, metrics in zip(report.fold_definitions, report.folds):
            span = (definition.test_to_ms - definition.test_from_ms) / 86_400_000
            print(f"  fold {definition.index} ({span:5.0f}d): {metrics.summary()}")
        if report.holdout is not None:
            print(f"\nfrozen holdout: {report.holdout.summary()}")
        print(
            f"fill rate: {report.fill_rate * 100:.1f}% "
            f"({report.fill_filled:,}/{report.fill_requested:,} intended trades happened)"
        )

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

        gates = promotion_gates(
            report.folds, report.holdout, stress_positive_folds=stress_positive
        )
        print("\npromotion gates:")
        for gate in gates:
            print(f"  {'PASS' if gate.passed else 'FAIL'}  {gate.name:32s} {gate.detail}")

        passed = all(gate.passed for gate in gates)
        print(f"\nRESULT [{name}]: {'PROMOTABLE' if passed else 'NOT SUPPORTED'}")
        if passed:
            exit_code = 0

        payload[name] = {
            "folds": [metrics.__dict__ for metrics in report.folds],
            "holdout": report.holdout.__dict__ if report.holdout else None,
            "gates": [gate.__dict__ for gate in gates],
            "promotable": passed,
        }

    print("\nNo Demo run is implied by any result here; this is offline evidence only.")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps({"parameters": parameters.__dict__, "results": payload},
                       indent=2, default=str)
        )
        print(f"wrote {args.json_out}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
