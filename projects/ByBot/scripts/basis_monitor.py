"""Watch the dated-futures basis curve on Bybit.

This is an observation tool, not a bot. It quotes every dated contract against
its reference leg, prices the carry after real book spreads, records what it
saw, and flags contracts that have left their own observed range.

Read-only: only public market endpoints are called, no credentials are loaded,
and there is no order path anywhere in app/basis.

    python scripts/basis_monitor.py --record \
        --database-url postgresql://.../bybot_claude
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.basis.monitor import BasisMonitor, curve_alerts  # noqa: E402
from app.history.client import BybitHistoryClient  # noqa: E402
from app.history.storage import HistoryStorage  # noqa: E402
from scripts.history_backfill import guard_research_database  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", default="perp", choices=["perp", "spot"])
    parser.add_argument("--base-coins", nargs="*", default=None,
                        help="restrict to these underlyings, e.g. BTC ETH")
    parser.add_argument("--min-capacity", type=float, default=0.0,
                        help="hide contracts thinner than this, in USD")
    parser.add_argument("--maker", action="store_true",
                        help="price the round trip at maker fees on the futures legs")
    parser.add_argument("--record", action="store_true", help="store this observation")
    parser.add_argument("--alert-threshold", type=float, default=100.0,
                        help="bps/yr deviation from a contract's own median")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--base-url", default="https://api.bybit.com")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    storage = None
    if args.record or args.database_url:
        if not args.database_url:
            raise SystemExit("--record needs --database-url or DATABASE_URL")
        storage = HistoryStorage(guard_research_database(args.database_url))
        storage.create_schema()

    client = BybitHistoryClient(args.base_url, min_request_interval_seconds=0.08)
    monitor = BasisMonitor(client)

    coins = set(args.base_coins) if args.base_coins else None
    observations = monitor.observe(reference_kind=args.reference, base_coins=coins)
    if not observations:
        print("no live dated contracts found")
        return 1

    visible = [o for o in observations if o.capacity_usd >= args.min_capacity]
    label = "maker" if args.maker else "taker"
    print(
        f"reference leg: {args.reference}   fees: {label}   "
        f"{len(visible)} of {len(observations)} contracts shown\n"
    )
    print(f"{'contract':22s} {'days':>5s} {'basis':>9s} {'annual':>9s} "
          f"{'cost':>7s} {'net/yr':>9s} {'capacity':>12s}")
    print("-" * 82)

    current_coin = None
    for item in visible:
        if item.base_coin != current_coin:
            current_coin = item.base_coin
            print(f"[{current_coin}]")
        print(
            f"  {item.future.symbol:20s} {item.days_to_delivery:5.0f} "
            f"{item.basis_bps:+8.1f}b {item.annualised_bps:+8.1f}b "
            f"{item.round_trip_bps(maker=args.maker):6.1f}b "
            f"{item.net_annualised_bps(maker=args.maker):+8.1f}b "
            f"{item.capacity_usd:11,.0f}$"
        )

    best = max(
        (o for o in visible if o.tradeable),
        key=lambda o: o.net_annualised_bps(maker=args.maker),
        default=None,
    )
    if best is not None:
        net = best.net_annualised_bps(maker=args.maker)
        print(
            f"\nbest net carry: {best.future.symbol} at {net:+.0f} bps/yr "
            f"({net / 100:+.2f}%/yr) over {best.days_to_delivery:.0f} days, "
            f"capacity ${best.capacity_usd:,.0f}"
        )

    if storage is not None:
        history = storage.basis_history(args.reference)
        alerts = curve_alerts(history, visible, threshold_bps=args.alert_threshold)
        if alerts:
            print("\ncurve alerts:")
            for alert in alerts:
                print(f"  {alert.describe()}")
        elif history:
            samples = sum(len(v) for v in history.values())
            print(f"\nno alerts; {samples:,} prior observations on record")
        else:
            print("\nno prior observations yet; a range needs several runs")

        if args.record:
            written = storage.write_basis(observations)
            print(f"recorded {written.inserted} observations")

    print(
        "\nThis is a rate, not an edge: the same carry is available to anyone "
        "and is priced against holding cash. No order is placed by this tool."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
