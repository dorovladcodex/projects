from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.v2.research import bootstrap_mean_confidence_interval


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only V2 alpha validation")
    parser.add_argument("--artifacts", required=True, help="Run-specific artifact directory")
    args = parser.parse_args()
    root = Path(args.artifacts).resolve()
    trades_path = root / "trades.csv"
    if not trades_path.exists():
        raise SystemExit("trades.csv is missing")
    with trades_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    pnl = [Decimal(row["net_pnl"]) for row in rows if row.get("net_pnl") not in (None, "")]
    lower, upper = bootstrap_mean_confidence_interval(pnl)
    result = {
        "trade_count": len(pnl),
        "net_pnl": str(sum(pnl, Decimal("0"))),
        "mean_net_pnl": str(sum(pnl, Decimal("0")) / Decimal(len(pnl))) if pnl else None,
        "bootstrap_95pct_lower": str(lower) if lower is not None else None,
        "bootstrap_95pct_upper": str(upper) if upper is not None else None,
        "evidence_gate_passed": bool(len(pnl) >= 200 and lower is not None and lower > 0),
        "read_only": True,
    }
    output = root / "alpha_validation.json"
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["evidence_gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
