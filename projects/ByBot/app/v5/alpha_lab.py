from __future__ import annotations

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.persistence import PersistenceRepository, V2MarketFeatureRow, V2UniverseStateRow
from app.v5.research import (
    LONG_HORIZONS_SECONDS,
    MomentumObservation,
    PricePoint,
    beta_hedged_pair_return_bps,
    build_non_overlapping_momentum,
    chronological_folds,
    cost_stress,
    decimal_or_none,
    economic_metrics,
    fit_beta_train_only,
    leave_group_out,
)


ZERO = Decimal("0")
ONE = Decimal("1")
REFERENCE_DIRECTIONAL_COST_BPS = Decimal("11")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_empty_csv(path: Path, fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        csv.DictWriter(handle, fieldnames=list(fields)).writeheader()


def _load_history(
    repository: PersistenceRepository,
) -> tuple[dict[str, list[PricePoint]], dict[str, Any]]:
    history: dict[str, list[PricePoint]] = {}
    raw_count = 0
    invalid_prices = 0
    funding_non_null = 0
    funding_values: dict[str, set[str]] = defaultdict(set)
    first: datetime | None = None
    last: datetime | None = None
    with Session(repository.engine) as session:
        symbols = list(session.scalars(
            select(distinct(V2MarketFeatureRow.symbol)).order_by(V2MarketFeatureRow.symbol)
        ))
        for symbol in symbols:
            points: list[PricePoint] = []
            rows = session.execute(
                select(V2MarketFeatureRow.captured_at, V2MarketFeatureRow.payload)
                .where(V2MarketFeatureRow.symbol == symbol)
                .order_by(V2MarketFeatureRow.captured_at)
            ).all()
            raw_count += len(rows)
            for captured_at, payload in rows:
                values = payload or {}
                price = decimal_or_none(values.get("last_price"))
                if price is None or price <= 0:
                    invalid_prices += 1
                    continue
                funding = decimal_or_none(values.get("funding_rate"))
                if funding is not None:
                    funding_non_null += 1
                    funding_values[symbol].add(str(funding))
                bid_depth = decimal_or_none(values.get("bid_depth_10bps_usdt"))
                ask_depth = decimal_or_none(values.get("ask_depth_10bps_usdt"))
                depth = (
                    min(bid_depth, ask_depth)
                    if bid_depth is not None and ask_depth is not None else None
                )
                points.append(PricePoint(
                    timestamp=captured_at,
                    price=price,
                    funding_rate=funding,
                    spread_bps=decimal_or_none(values.get("spread_bps")),
                    depth_usdt=depth,
                ))
                first = captured_at if first is None else min(first, captured_at)
                last = captured_at if last is None else max(last, captured_at)
            history[symbol] = points
    return history, {
        "raw_feature_snapshot_count": raw_count,
        "valid_price_snapshot_count": sum(len(rows) for rows in history.values()),
        "invalid_price_snapshot_count": invalid_prices,
        "symbol_count": len(history),
        "symbols": sorted(history),
        "range_start": first,
        "range_end": last,
        "range_days": (
            Decimal(str((last - first).total_seconds())) / Decimal("86400")
            if first is not None and last is not None else None
        ),
        "funding_non_null_snapshot_count": funding_non_null,
        "distinct_polled_funding_values_by_symbol": {
            symbol: len(values) for symbol, values in sorted(funding_values.items())
        },
    }


def _universe_evidence(repository: PersistenceRepository) -> dict[str, Any]:
    with Session(repository.engine) as session:
        rows = list(session.scalars(select(V2UniverseStateRow)))
    instrument_keys: set[str] = set()
    categories: set[str] = set()
    for row in rows:
        instrument = (row.payload or {}).get("instrument") or {}
        instrument_keys.update(instrument)
        if instrument.get("category") is not None:
            categories.add(str(instrument["category"]))
    return {
        "row_count": len(rows),
        "accepted_count": sum(1 for row in rows if row.accepted),
        "instrument_fields": sorted(instrument_keys),
        "category_values": sorted(categories),
        "classification": "PARTIAL",
        "reason": "linear universe metadata exists; no persisted spot instrument universe",
    }


def _audit(
    repository: PersistenceRepository,
    history_summary: dict[str, Any],
    universe: dict[str, Any],
    settings: Settings,
    v4_dir: Path,
    database_revision: str | None,
) -> tuple[dict[str, Any], str]:
    v4_tape = v4_dir / "opportunity-tape.csv"
    v4_labels = v4_dir / "forward-labels.csv"
    fee_values = {
        "spot_maker": settings.v5_spot_maker_fee_bps,
        "spot_taker": settings.v5_spot_taker_fee_bps,
        "perp_maker": settings.v5_perp_maker_fee_bps,
        "perp_taker": settings.v5_perp_taker_fee_bps,
        "spot_borrow_apr": settings.v5_spot_borrow_apr_bps,
    }
    coverage = {
        "v4_opportunity_tape": {
            "status": "AVAILABLE" if v4_tape.exists() else "MISSING",
            "bytes": v4_tape.stat().st_size if v4_tape.exists() else 0,
        },
        "v4_forward_labels": {
            "status": "AVAILABLE" if v4_labels.exists() else "MISSING",
            "bytes": v4_labels.stat().st_size if v4_labels.exists() else 0,
        },
        "perpetual_last_price_history": {
            "status": "AVAILABLE",
            "rows": history_summary["valid_price_snapshot_count"],
            "range_start": history_summary["range_start"],
            "range_end": history_summary["range_end"],
        },
        "perpetual_top_of_book_and_depth": {
            "status": "AVAILABLE",
            "detail": "bid/ask/spread and 10bps depth are persisted in V2 snapshots",
        },
        "current_funding_polled_history": {
            "status": "PARTIAL",
            "detail": "latest current rate was repeatedly polled; exact funding events/interval history were not stored",
        },
        "predicted_funding_history": {"status": "MISSING"},
        "next_funding_time_history": {"status": "MISSING"},
        "funding_interval_history": {"status": "MISSING"},
        "spot_prices_and_books": {"status": "MISSING"},
        "mark_price_history": {"status": "MISSING"},
        "index_price_history": {"status": "MISSING"},
        "premium_index_history": {"status": "MISSING"},
        "basis_history": {"status": "MISSING"},
        "borrow_cost_history": {"status": "MISSING"},
        "account_specific_fee_configuration": {
            "status": "AVAILABLE" if all(value is not None for value in fee_values.values()) else "MISSING",
            "values": fee_values,
            "detail": "V2 fee defaults are not substituted for V5 account-specific carry fees",
        },
        "maker_fill_and_adverse_selection_telemetry": {"status": "MISSING"},
        "instrument_metadata": universe,
        "strategy_interfaces": {
            "status": "AVAILABLE",
            "detail": "V2 strategy/candidate abstractions preserved; V5 research does not route into them",
        },
        "portfolio_and_risk": {
            "status": "AVAILABLE",
            "detail": "existing abstractions preserved and deliberately absent from V5 shadow collector",
        },
        "research_validation": {
            "status": "AVAILABLE",
            "detail": "V4 opportunity tape, forward labels, chronological folds, frozen holdout and cost stress preserved",
        },
    }
    markdown = "\n".join((
        "# ByBot V5 Alpha Lab audit",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "The historical store supports perpetual-only, long-horizon and cross-sectional research. It does not support an exact spot/perpetual carry backtest because the spot leg, mark/index series, funding-event history, predicted funding, funding schedule and account-specific multi-leg fees are absent.",
        "",
        "No missing carry field was imputed. V5 carry outputs remain header-only/INSUFFICIENT_DATA until the shadow contract collects synchronized authoritative inputs.",
        "",
        f"Market rows: {history_summary['valid_price_snapshot_count']}; symbols: {history_summary['symbol_count']}; range: {history_summary['range_start']} to {history_summary['range_end']}.",
        "",
        f"Migration 20260811_0015 is additive and backward-compatible in code, but the working database remains at {database_revision or 'UNKNOWN'}. It was not applied during this read-only milestone.",
    )) + "\n"
    return coverage, markdown


def _momentum_analysis(
    history: dict[str, list[PricePoint]],
) -> tuple[dict[str, Any], dict[int, list[MomentumObservation]]]:
    by_horizon: dict[str, Any] = {}
    observations_by_horizon: dict[int, list[MomentumObservation]] = {}
    for horizon in LONG_HORIZONS_SECONDS:
        observations = sorted(
            (
                row
                for symbol, points in history.items()
                for row in build_non_overlapping_momentum(
                    symbol, points,
                    horizon_seconds=horizon,
                    reference_cost_bps=REFERENCE_DIRECTIONAL_COST_BPS,
                )
            ),
            key=lambda row: (row.timestamp, row.symbol),
        )
        observations_by_horizon[horizon] = observations
        split = chronological_folds(
            observations, folds=4, purge_seconds=horizon,
        )
        oos = [row for fold in split["folds"] for row in fold["validation"]]
        holdout = split["holdout"]
        concentration = _momentum_concentration(oos)
        folds = [
            {
                "fold": fold["fold"],
                "train_count": len(fold["train"]),
                "validation_start": fold["validation_start"],
                "validation": economic_metrics(
                    [row.gross_strategy_bps for row in fold["validation"]],
                    cost_bps=REFERENCE_DIRECTIONAL_COST_BPS,
                ),
                "fit_scope": "TRAIN_ONLY",
            }
            for fold in split["folds"]
        ]
        by_horizon[str(horizon)] = {
            "horizon": _horizon_name(horizon),
            "observation_rule": "one observation per symbol per horizon; no within-horizon overlap",
            "total_observations": len(observations),
            "chronological_oos": economic_metrics(
                [row.gross_strategy_bps for row in oos],
                cost_bps=REFERENCE_DIRECTIONAL_COST_BPS,
            ),
            "frozen_holdout": economic_metrics(
                [row.gross_strategy_bps for row in holdout],
                cost_bps=REFERENCE_DIRECTIONAL_COST_BPS,
            ),
            "folds": folds,
            "cost_stress": cost_stress(
                [row.gross_strategy_bps for row in oos],
                base_cost_bps=REFERENCE_DIRECTIONAL_COST_BPS,
            ),
            "robustness": {
                "leave_day_out": leave_group_out(
                    oos, group="day", cost_bps=REFERENCE_DIRECTIONAL_COST_BPS,
                ),
                "leave_week_out": leave_group_out(
                    oos, group="week", cost_bps=REFERENCE_DIRECTIONAL_COST_BPS,
                ),
                "leave_symbol_out": leave_group_out(
                    oos, group="symbol", cost_bps=REFERENCE_DIRECTIONAL_COST_BPS,
                ),
                "concentration": concentration,
            },
            "holdout_frozen_before_horizon_selection": True,
            "random_shuffle": False,
        }
    candidates = [
        (
            horizon,
            decimal_or_none(result["chronological_oos"]["net_expectancy_bps"]),
            int(result["chronological_oos"]["observation_count"]),
        )
        for horizon, result in ((int(key), value) for key, value in by_horizon.items())
    ]
    exploratory_horizon, _, _ = max(
        candidates,
        key=lambda item: item[1] if item[1] is not None else Decimal("-Infinity"),
    )
    evidence_eligible = [item for item in candidates if item[2] >= 100]
    selection_pool = evidence_eligible or candidates
    best_horizon, _, _ = max(
        selection_pool,
        key=lambda item: item[1] if item[1] is not None else Decimal("-Infinity"),
    )
    return {
        "family": "LONG_HORIZON_MOMENTUM",
        "reference_cost_bps": str(REFERENCE_DIRECTIONAL_COST_BPS),
        "reference_cost_note": "historical directional all-in reference; not an account fee assertion",
        "horizons": by_horizon,
        "selected_on_development_only": _horizon_name(best_horizon),
        "selected_horizon_seconds": best_horizon,
        "selection_minimum_oos_observations": 100,
        "exploratory_best_any_sample": _horizon_name(exploratory_horizon),
        "exploratory_best_is_selection_eligible": exploratory_horizon == best_horizon,
        "selected_frozen_holdout": by_horizon[str(best_horizon)]["frozen_holdout"],
        "no_lookahead": True,
    }, observations_by_horizon


def _momentum_concentration(rows: Sequence[MomentumObservation]) -> dict[str, Any]:
    total_positive = sum((max(row.net_strategy_bps, ZERO) for row in rows), ZERO)
    output: dict[str, Any] = {}
    for dimension in ("symbol", "day"):
        grouped: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for row in rows:
            key = row.symbol if dimension == "symbol" else row.timestamp.date().isoformat()
            grouped[key] += max(row.net_strategy_bps, ZERO)
        largest_key = max(grouped, key=grouped.get) if grouped else None
        largest_value = grouped.get(largest_key, ZERO) if largest_key is not None else ZERO
        output[dimension] = {
            "largest_positive_group": largest_key,
            "largest_positive_net_bps": str(largest_value),
            "share_of_positive_net": (
                str(largest_value / total_positive) if total_positive > 0 else None
            ),
        }
    return output


def _horizon_name(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


@dataclass(frozen=True)
class PairCandidate:
    timestamp: datetime
    horizon_seconds: int
    long_symbol: str
    short_symbol: str
    long_return_bps: Decimal
    short_return_bps: Decimal


@dataclass(frozen=True)
class PairResult:
    timestamp: datetime
    long_symbol: str
    short_symbol: str
    gross_bps: Decimal
    cost_bps: Decimal
    net_bps: Decimal
    hedge_ratio: Decimal


def _pair_metrics(rows: Sequence[PairResult], *, cost_multiplier: Decimal = Decimal("1")) -> dict[str, Any]:
    net = [row.gross_bps - row.cost_bps * cost_multiplier for row in rows]
    gross = [row.gross_bps for row in rows]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value < 0]
    equity = ZERO
    peak = ZERO
    drawdown = ZERO
    for value in net:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "observation_count": len(rows),
        "gross_expectancy_bps_on_long_leg_capital": (
            str(sum(gross, ZERO) / Decimal(len(gross))) if gross else None
        ),
        "net_expectancy_bps_on_long_leg_capital": (
            str(sum(net, ZERO) / Decimal(len(net))) if net else None
        ),
        "win_rate": str(Decimal(len(wins)) / Decimal(len(net))) if net else None,
        "profit_factor": str(sum(wins, ZERO) / abs(sum(losses, ZERO))) if losses else None,
        "maximum_drawdown_bps": str(drawdown),
        "average_two_leg_cost_bps": (
            str(sum((row.cost_bps * cost_multiplier for row in rows), ZERO) / Decimal(len(rows)))
            if rows else None
        ),
        "cost_multiplier": str(cost_multiplier),
    }


def _pair_robustness(rows: Sequence[PairResult]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for dimension in ("day", "week", "symbol"):
        groups: set[str] = set()
        for row in rows:
            if dimension == "day":
                groups.add(row.timestamp.date().isoformat())
            elif dimension == "week":
                iso = row.timestamp.isocalendar()
                groups.add(f"{iso.year}-W{iso.week:02d}")
            else:
                groups.update((row.long_symbol, row.short_symbol))
        dimension_rows: dict[str, Any] = {}
        for omitted in sorted(groups):
            kept = []
            for row in rows:
                if dimension == "day" and row.timestamp.date().isoformat() == omitted:
                    continue
                if dimension == "week":
                    iso = row.timestamp.isocalendar()
                    if f"{iso.year}-W{iso.week:02d}" == omitted:
                        continue
                if dimension == "symbol" and omitted in {row.long_symbol, row.short_symbol}:
                    continue
                kept.append(row)
            dimension_rows[omitted] = _pair_metrics(kept)
        output[f"leave_{dimension}_out"] = dimension_rows
    return output


def _pair_candidates(
    observations: Sequence[MomentumObservation], horizon: int,
) -> tuple[list[PairCandidate], dict[int, dict[str, MomentumObservation]]]:
    buckets: dict[int, dict[str, MomentumObservation]] = defaultdict(dict)
    for row in observations:
        bucket = int(row.timestamp.timestamp()) // horizon
        buckets[bucket][row.symbol] = row
    candidates: list[PairCandidate] = []
    for bucket, rows in sorted(buckets.items()):
        eligible = [row for row in rows.values() if row.symbol != "BTCUSDT"]
        if len(eligible) < 4 or "BTCUSDT" not in rows:
            continue
        ranked = sorted(eligible, key=lambda row: (row.past_return_bps, row.symbol))
        weakest, strongest = ranked[0], ranked[-1]
        if strongest.symbol == weakest.symbol:
            continue
        candidates.append(PairCandidate(
            timestamp=max(row.timestamp for row in rows.values()),
            horizon_seconds=horizon,
            long_symbol=strongest.symbol,
            short_symbol=weakest.symbol,
            long_return_bps=strongest.future_return_bps,
            short_return_bps=weakest.future_return_bps,
        ))
    return candidates, buckets


def _fit_betas(
    buckets: dict[int, dict[str, MomentumObservation]], *, before: datetime,
) -> dict[str, Decimal]:
    asset: dict[str, list[Decimal]] = defaultdict(list)
    market: dict[str, list[Decimal]] = defaultdict(list)
    for rows in buckets.values():
        btc = rows.get("BTCUSDT")
        if btc is None or btc.timestamp >= before:
            continue
        for symbol, row in rows.items():
            if symbol == "BTCUSDT":
                continue
            asset[symbol].append(row.future_return_bps)
            market[symbol].append(btc.future_return_bps)
    output: dict[str, Decimal] = {}
    for symbol in asset:
        beta = fit_beta_train_only(asset[symbol], market[symbol])
        if beta is not None:
            output[symbol] = beta
    return output


def _evaluate_pairs(
    candidates: Sequence[PairCandidate], betas: dict[str, Decimal],
) -> tuple[list[PairResult], Counter[str]]:
    output: list[PairResult] = []
    skipped: Counter[str] = Counter()
    for row in candidates:
        long_beta = betas.get(row.long_symbol)
        short_beta = betas.get(row.short_symbol)
        if long_beta is None or short_beta is None:
            skipped["BETA_MISSING"] += 1
            continue
        try:
            gross, hedge_ratio = beta_hedged_pair_return_bps(
                long_return_bps=row.long_return_bps,
                short_return_bps=row.short_return_bps,
                long_beta=long_beta,
                short_beta=short_beta,
            )
        except ValueError:
            skipped["BETA_HEDGE_INVALID"] += 1
            continue
        # Each leg pays the historical round-trip directional reference on its
        # own notional. Reporting is per one unit of long-leg capital.
        two_leg_cost = REFERENCE_DIRECTIONAL_COST_BPS * (Decimal("1") + abs(hedge_ratio))
        output.append(PairResult(
            timestamp=row.timestamp,
            long_symbol=row.long_symbol,
            short_symbol=row.short_symbol,
            gross_bps=gross,
            cost_bps=two_leg_cost,
            net_bps=gross - two_leg_cost,
            hedge_ratio=hedge_ratio,
        ))
    return output, skipped


def _relative_value_analysis(
    observations_by_horizon: dict[int, list[MomentumObservation]],
) -> dict[str, Any]:
    horizons: dict[str, Any] = {}
    for horizon, observations in observations_by_horizon.items():
        candidates, buckets = _pair_candidates(observations, horizon)
        split = chronological_folds(candidates, folds=4, purge_seconds=horizon)
        oos_results: list[PairResult] = []
        fold_rows = []
        skipped_total: Counter[str] = Counter()
        for fold in split["folds"]:
            before = fold["validation"][0].timestamp
            betas = _fit_betas(buckets, before=before)
            evaluated, skipped = _evaluate_pairs(fold["validation"], betas)
            oos_results.extend(evaluated)
            skipped_total.update(skipped)
            fold_rows.append({
                "fold": fold["fold"],
                "validation_start": fold["validation_start"],
                "beta_fit_scope": "TRAIN_ONLY",
                "beta_symbol_count": len(betas),
                "metrics": _pair_metrics(evaluated),
                "skipped": dict(skipped),
            })
        holdout = split["holdout"]
        holdout_start = holdout[0].timestamp if holdout else datetime.max.replace(tzinfo=timezone.utc)
        holdout_betas = _fit_betas(buckets, before=holdout_start)
        holdout_results, holdout_skipped = _evaluate_pairs(holdout, holdout_betas)
        pair_counts = Counter(
            f"{row.long_symbol}/{row.short_symbol}" for row in oos_results
        )
        robustness = _pair_robustness(oos_results)
        horizons[str(horizon)] = {
            "horizon": _horizon_name(horizon),
            "candidate_count": len(candidates),
            "chronological_oos": _pair_metrics(oos_results),
            "frozen_holdout": _pair_metrics(holdout_results),
            "folds": fold_rows,
            "cost_stress": {
                f"{multiplier}x": _pair_metrics(oos_results, cost_multiplier=multiplier)
                for multiplier in (Decimal("1"), Decimal("1.25"), Decimal("1.5"), Decimal("2"))
            },
            "pair_distribution": dict(pair_counts),
            "largest_pair_share": (
                str(Decimal(max(pair_counts.values())) / Decimal(sum(pair_counts.values())))
                if pair_counts else None
            ),
            "skipped": dict(skipped_total),
            "holdout_skipped": dict(holdout_skipped),
            "robustness": robustness,
            "funding_difference": "UNKNOWN",
            "rebalance_turnover": "UNKNOWN_NO_HISTORICAL_REBALANCE_TELEMETRY",
            "hedge_ratios_fit_scope": "TRAIN_ONLY_PER_FOLD",
            "equal_dollar_neutrality_assumed": False,
        }
    ranked = [
        (
            int(key),
            decimal_or_none(value["chronological_oos"]["net_expectancy_bps_on_long_leg_capital"]),
            int(value["chronological_oos"]["observation_count"]),
        )
        for key, value in horizons.items()
    ]
    exploratory, _, _ = max(
        ranked,
        key=lambda item: item[1] if item[1] is not None else Decimal("-Infinity"),
    )
    eligible = [item for item in ranked if item[2] >= 100]
    selection_pool = eligible or ranked
    selected, _, _ = max(
        selection_pool,
        key=lambda item: item[1] if item[1] is not None else Decimal("-Infinity"),
    )
    return {
        "family": "RELATIVE_VALUE_BETA_HEDGED_CROSS_SECTIONAL_MOMENTUM",
        "horizons": horizons,
        "selected_on_development_only": _horizon_name(selected),
        "selected_horizon_seconds": selected,
        "selection_minimum_oos_observations": 100,
        "exploratory_best_any_sample": _horizon_name(exploratory),
        "exploratory_best_is_selection_eligible": exploratory == selected,
        "selected_frozen_holdout": horizons[str(selected)]["frozen_holdout"],
        "two_leg_costs_included": True,
        "no_lookahead": True,
    }


def _load_v4_baseline(v4_dir: Path) -> dict[str, Any]:
    path = v4_dir / "recommendation.json"
    if not path.exists():
        return {"status": "MISSING"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        "status": payload.get("status"),
        "oos_observations": payload.get("oos_selected_observations"),
        "gross_edge_bps": payload.get("gross_edge_oos_bps"),
        "net_edge_bps": payload.get("net_expectancy_11bps"),
        "profit_factor": payload.get("oos_profit_factor"),
        "maximum_drawdown_bps": payload.get("maximum_oos_drawdown_bps"),
        "holdout_positive": payload.get("frozen_holdout_confirmed"),
    }


def _family_gate(metrics: dict[str, Any], stress: dict[str, Any], *, pair: bool = False) -> dict[str, Any]:
    net_key = "net_expectancy_bps_on_long_leg_capital" if pair else "net_expectancy_bps"
    observations = int(metrics.get("observation_count") or 0)
    net = decimal_or_none(metrics.get(net_key))
    pf = decimal_or_none(metrics.get("profit_factor"))
    stress_metrics = stress.get("1.5x") or stress.get("1.50x") or {}
    stress_net = decimal_or_none(stress_metrics.get(net_key))
    gates = {
        "minimum_100_oos_observations": observations >= 100,
        "positive_chronological_oos_net": net is not None and net > 0,
        "profit_factor_materially_above_one": pf is not None and pf >= Decimal("1.20"),
        "positive_at_1_5x_cost": stress_net is not None and stress_net > 0,
    }
    return {"passed": all(gates.values()), "gates": gates}


def run_alpha_lab_v5(
    *,
    repository: PersistenceRepository,
    settings: Settings,
    output_dir: Path,
    v4_artifact_dir: Path,
) -> dict[str, Any]:
    if not repository.available:
        raise RuntimeError("database persistence is unavailable")
    output_dir.mkdir(parents=True, exist_ok=True)
    with repository.engine.connect() as connection:
        try:
            database_revision = connection.exec_driver_sql(
                "select version_num from alembic_version"
            ).scalar()
        except Exception:
            database_revision = None
    history, history_summary = _load_history(repository)
    universe = _universe_evidence(repository)
    coverage, audit_markdown = _audit(
        repository, history_summary, universe, settings, v4_artifact_dir,
        str(database_revision) if database_revision is not None else None,
    )
    write_json(output_dir / "data-coverage.json", {
        "generated_at": datetime.now(timezone.utc),
        "history": history_summary,
        "coverage": coverage,
        "no_imputation": True,
    })
    (output_dir / "audit.md").write_text(audit_markdown, encoding="utf-8")

    carry_fields = (
        "opportunity_id", "timestamp", "symbol", "spot_symbol", "perp_symbol",
        "spot_bid", "spot_ask", "spot_mid", "perp_bid", "perp_ask", "perp_mid",
        "mark_price", "index_price", "basis_bps", "current_funding_rate",
        "predicted_funding_rate", "next_funding_time", "funding_interval_hours",
        "historical_funding_previous", "historical_funding_24h",
        "historical_funding_3d", "historical_funding_7d", "spot_spread_bps",
        "perp_spread_bps", "spot_depth", "perp_depth", "availability", "blockers",
    )
    carry_label_fields = (
        "opportunity_id", "symbol", "horizon", "horizon_end", "coverage",
        "funding_income", "basis_change_pnl", "spot_leg_pnl", "perp_leg_pnl",
        "hedged_gross_pnl", "entry_cost", "exit_cost", "estimated_slippage",
        "funding_received", "funding_paid", "net_carry_pnl", "net_carry_bps",
        "max_basis_adverse_excursion_bps", "max_hedge_imbalance_bps",
        "funding_sign_flip", "time_to_break_even_seconds", "blockers",
    )
    write_empty_csv(output_dir / "carry-opportunities.csv", carry_fields)
    write_empty_csv(output_dir / "carry-labels.csv", carry_label_fields)

    carry_status = {
        "status": "INSUFFICIENT_DATA",
        "opportunity_count": 0,
        "label_count": 0,
        "blockers": [
            "NO_HISTORICAL_SPOT_PRICES_OR_BOOKS",
            "NO_HISTORICAL_MARK_INDEX_OR_BASIS",
            "NO_EXACT_FUNDING_EVENT_AND_INTERVAL_HISTORY",
            "NO_PREDICTED_FUNDING_HISTORY",
            "NO_ACCOUNT_SPECIFIC_MULTI_LEG_FEE_CONFIGURATION",
            "NO_MAKER_FILL_OR_ADVERSE_SELECTION_TELEMETRY",
        ],
        "invented_rows": 0,
    }
    funding_persistence = {
        **carry_status,
        "question": "Can elevated funding persist long enough to amortize multi-leg costs?",
        "answer": "UNKNOWN",
        "available_current_rate_poll_count": history_summary["funding_non_null_snapshot_count"],
        "warning": "Repeated current-rate polls are not treated as authoritative funding payments.",
        "planned_predictors": [
            "current_funding", "funding_acceleration", "funding_persistence",
            "premium_index", "basis", "open_interest", "volatility", "volume",
            "market_regime",
        ],
        "planned_targets": [
            "future_cumulative_funding_1_period", "future_cumulative_funding_2_periods",
            "future_cumulative_funding_3_periods", "future_cumulative_funding_6_periods",
            "net_carry_after_all_costs",
        ],
        "no_trade_meta_model": "NOT_FIT_WITHOUT_EXACT_TARGETS",
    }
    basis_analysis = {
        **carry_status,
        "basis_convergence_contribution": "UNKNOWN",
    }
    carry_cost = {
        **carry_status,
        "account_fee_configuration": coverage["account_specific_fee_configuration"],
        "notional_scenarios_usdt": [100, 200, 500, 1000],
        "component_model_ready": True,
        "components": [
            "spot_entry_fee", "spot_exit_fee", "perp_entry_fee", "perp_exit_fee",
            "spot_spread", "perp_spread", "spot_slippage", "perp_slippage",
            "rebalance_cost", "borrow_cost_when_applicable",
        ],
        "stress_multipliers": ["1x", "1.25x", "1.5x", "2x"],
        "economics_computed": False,
    }
    carry_walk_forward = {
        **carry_status,
        "requested_horizons": [
            "1_funding_interval", "2_funding_intervals", "3_funding_intervals",
            "6_funding_intervals", "12h", "24h", "48h", "72h",
        ],
        "chronological_folds": 0,
        "frozen_holdout": True,
        "promotion_evidence": False,
    }
    write_json(output_dir / "funding-persistence.json", funding_persistence)
    write_json(output_dir / "basis-analysis.json", basis_analysis)
    write_json(output_dir / "carry-cost-analysis.json", carry_cost)
    write_json(output_dir / "carry-walk-forward.json", carry_walk_forward)

    momentum, observations_by_horizon = _momentum_analysis(history)
    relative = _relative_value_analysis(observations_by_horizon)
    write_json(output_dir / "long-horizon-momentum.json", momentum)
    write_json(output_dir / "relative-value-analysis.json", relative)

    execution = {
        "status": "INSUFFICIENT_EXECUTION_DATA",
        "scenarios": {
            name: {
                "status": "INSUFFICIENT_EXECUTION_DATA",
                "maker_fill_assumed": False,
                "missing": (
                    ["ACCOUNT_FEES", "SPOT_PERP_SLIPPAGE"]
                    if name == "TAKER_TAKER"
                    else [
                        "ACCOUNT_FEES", "SPOT_PERP_SLIPPAGE",
                        "MAKER_FILL_PROBABILITY", "MAKER_ADVERSE_SELECTION",
                    ]
                ),
            }
            for name in (
                "TAKER_TAKER", "MAKER_TAKER", "TAKER_MAKER", "MAKER_MAKER",
                "MAKER_WITH_BOUNDED_TAKER_FALLBACK",
            )
        },
        "shadow_contract": "app.v5.shadow.V5CarryShadowCollector",
    }
    write_json(output_dir / "execution-scenarios.json", execution)

    selected_momentum = momentum["horizons"][str(momentum["selected_horizon_seconds"])]
    selected_relative = relative["horizons"][str(relative["selected_horizon_seconds"])]
    stresses = {
        "FUNDING_CARRY": {"status": "INSUFFICIENT_DATA"},
        "LONG_HORIZON_MOMENTUM": selected_momentum["cost_stress"],
        "RELATIVE_VALUE": selected_relative["cost_stress"],
    }
    write_json(output_dir / "cost-stress.json", stresses)

    momentum_gate = _family_gate(
        selected_momentum["chronological_oos"], selected_momentum["cost_stress"],
    )
    momentum_holdout_net = decimal_or_none(
        selected_momentum["frozen_holdout"].get("net_expectancy_bps")
    )
    momentum_gate["gates"]["positive_untouched_holdout"] = (
        momentum_holdout_net is not None and momentum_holdout_net > 0
    )
    momentum_positive_folds = sum(
        1 for fold in selected_momentum["folds"]
        if (decimal_or_none(fold["validation"].get("net_expectancy_bps")) or ZERO) > 0
    )
    momentum_concentration = selected_momentum["robustness"]["concentration"]
    momentum_largest_share = max(
        (
            decimal_or_none(momentum_concentration[key].get("share_of_positive_net")) or ONE
            for key in ("symbol", "day")
        ),
        default=ONE,
    )
    momentum_gate["gates"]["at_least_3_of_4_positive_folds"] = momentum_positive_folds >= 3
    momentum_gate["gates"]["no_symbol_or_day_over_50pct_positive_net"] = (
        momentum_largest_share <= Decimal("0.50")
    )
    momentum_gate["passed"] = all(momentum_gate["gates"].values())
    relative_gate = _family_gate(
        selected_relative["chronological_oos"], selected_relative["cost_stress"],
        pair=True,
    )
    relative_holdout_net = decimal_or_none(
        selected_relative["frozen_holdout"].get("net_expectancy_bps_on_long_leg_capital")
    )
    relative_gate["gates"]["positive_untouched_holdout"] = (
        relative_holdout_net is not None and relative_holdout_net > 0
    )
    relative_positive_folds = sum(
        1 for fold in selected_relative["folds"]
        if (
            decimal_or_none(
                fold["metrics"].get("net_expectancy_bps_on_long_leg_capital")
            ) or ZERO
        ) > 0
    )
    relative_largest_pair_share = (
        decimal_or_none(selected_relative.get("largest_pair_share")) or ONE
    )
    relative_gate["gates"]["at_least_3_of_4_positive_folds"] = relative_positive_folds >= 3
    relative_gate["gates"]["no_pair_over_50pct_observations"] = (
        relative_largest_pair_share <= Decimal("0.50")
    )
    relative_gate["passed"] = all(relative_gate["gates"].values())
    gates = {
        "FUNDING_CARRY": {
            "passed": False,
            "gates": {"sufficient_exact_historical_data": False},
            "status": "INSUFFICIENT_DATA",
        },
        "LONG_HORIZON_MOMENTUM": momentum_gate,
        "RELATIVE_VALUE": relative_gate,
    }
    write_json(output_dir / "promotion-gates.json", gates)

    v4 = _load_v4_baseline(v4_artifact_dir)
    family_comparison = {
        "CURRENT_DIRECTIONAL_V4": {
            **v4,
            "economic_mechanism": "tiny short-horizon directional prediction",
            "turnover": "HIGH",
            "capital_efficiency": "HIGH_IF_EDGE_EXISTED",
            "implementation_difficulty": "EXISTING",
            "execution_risk": "SINGLE_LEG_HIGH_TURNOVER",
            "cost_sensitivity": "ECONOMICALLY_REJECTED_AT_11_BPS_REFERENCE",
            "data_quality": "PARTIAL_BUT_SUBSTANTIAL",
            "overfitting_risk": "HIGH",
        },
        "FUNDING_CARRY": {
            "status": "INSUFFICIENT_DATA",
            "economic_mechanism": "funding plus basis convergence with delta-neutral spot/perp legs",
            "oos_observations": 0,
            "gross_edge_bps": None, "net_edge_bps": None, "profit_factor": None,
            "expectancy_bps": None, "maximum_drawdown_bps": None,
            "turnover": "LOW_TO_MEDIUM",
            "capital_efficiency": "UNKNOWN_SPOT_CAPITAL_AND_BORROW_CONSTRAINTS",
            "implementation_difficulty": "HIGH_TWO_VENUES_OR_TWO_MARKET_CATEGORIES",
            "execution_risk": "PARTIAL_FILL_BASIS_FUNDING_REVERSAL",
            "cost_sensitivity": "UNKNOWN_COMPONENT_INPUTS_MISSING",
            "data_quality": "MISSING_PRIMARY_HISTORY",
            "overfitting_risk": "UNKNOWN",
        },
        "LONG_HORIZON_MOMENTUM": {
            "status": "CANDIDATE" if momentum_gate["passed"] else "RESEARCH_ONLY",
            "economic_mechanism": "larger directional move at lower turnover",
            **selected_momentum["chronological_oos"],
            "frozen_holdout": selected_momentum["frozen_holdout"],
            "turnover": "LOW",
            "capital_efficiency": "SINGLE_LEG",
            "implementation_difficulty": "MEDIUM",
            "execution_risk": "DIRECTIONAL_GAP_AND_REGIME_REVERSAL",
            "cost_sensitivity": selected_momentum["cost_stress"],
            "data_quality": "AVAILABLE_BUT_SHORT_CALENDAR_RANGE",
            "overfitting_risk": "MEDIUM",
        },
        "RELATIVE_VALUE": {
            "status": "CANDIDATE" if relative_gate["passed"] else "RESEARCH_ONLY",
            "economic_mechanism": "training-only beta-hedged strongest versus weakest",
            **selected_relative["chronological_oos"],
            "frozen_holdout": selected_relative["frozen_holdout"],
            "turnover": "LOW_TO_MEDIUM",
            "capital_efficiency": "TWO_LEG",
            "implementation_difficulty": "HIGH",
            "execution_risk": "TWO_LEG_FILL_AND_HEDGE_ERROR",
            "cost_sensitivity": selected_relative["cost_stress"],
            "data_quality": "PARTIAL_NO_FUNDING_DIFFERENCE_OR_REBALANCE_HISTORY",
            "overfitting_risk": "MEDIUM_HIGH",
        },
    }
    write_json(output_dir / "family-comparison.json", family_comparison)

    any_supported = momentum_gate["passed"] or relative_gate["passed"]
    momentum_development_net = decimal_or_none(
        selected_momentum["chronological_oos"].get("net_expectancy_bps")
    )
    relative_development_net = decimal_or_none(
        selected_relative["chronological_oos"].get(
            "net_expectancy_bps_on_long_leg_capital"
        )
    )
    any_preliminary = bool(
        (
            momentum_development_net is not None
            and momentum_development_net > 0
            and momentum_holdout_net is not None
            and momentum_holdout_net > 0
        )
        or (
            relative_development_net is not None
            and relative_development_net > 0
            and relative_holdout_net is not None
            and relative_holdout_net > 0
        )
    )
    status = (
        "V5_CANDIDATE_SUPPORTED" if any_supported
        else "V5_PRELIMINARY_ONLY" if any_preliminary
        else "INSUFFICIENT_DATA_CONTINUE_SHADOW"
    )
    strongest = (
        "LONG_HORIZON_MOMENTUM" if momentum_gate["passed"]
        else "RELATIVE_VALUE" if relative_gate["passed"]
        else "NO_PROMOTABLE_FAMILY"
    )
    answers = {
        "1_funding_carry_positive_oos_after_cost": "UNKNOWN_INSUFFICIENT_DATA",
        "2_funding_persistent_enough": "UNKNOWN_INSUFFICIENT_EXACT_FUNDING_EVENTS",
        "3_best_holding_horizon_oos": {
            "carry": "UNKNOWN",
            "long_horizon_momentum": momentum["selected_on_development_only"],
            "relative_value": relative["selected_on_development_only"],
        },
        "4_stable_carry_symbols": "UNKNOWN_NO_VALID_CARRY_ROWS",
        "5_funding_reversal_before_break_even": "UNKNOWN",
        "6_basis_convergence_contribution": "UNKNOWN",
        "7_maker_benefit_after_adverse_selection": "UNKNOWN_INSUFFICIENT_EXECUTION_DATA",
        "8_long_horizon_momentum_beats_costs": momentum_gate["passed"],
        "9_relative_value_beats_two_leg_costs": relative_gate["passed"],
        "10_strongest_economic_evidence": strongest,
        "11_strongest_oos_pf_expectancy_drawdown": (
            selected_momentum["chronological_oos"] if strongest == "LONG_HORIZON_MOMENTUM"
            else selected_relative["chronological_oos"] if strongest == "RELATIVE_VALUE"
            else None
        ),
        "12_survives_stressed_costs": any_supported,
        "13_final_holdout_confirms": (
            momentum_gate["gates"]["positive_untouched_holdout"]
            if strongest == "LONG_HORIZON_MOMENTUM"
            else relative_gate["gates"]["positive_untouched_holdout"]
            if strongest == "RELATIVE_VALUE" else False
        ),
        "14_realistically_deployable_capital": "0 USDT certified; spot depth, account fees and carry execution telemetry are missing",
        "15_ready_for_short_demo_economics_canary": False,
    }
    recommendation = {
        "generated_at": datetime.now(timezone.utc),
        "status": status,
        "strongest_supported_family": strongest,
        "answers": answers,
        "promotion_gates": gates,
        "exchange_mutations": False,
        "database_mutations": False,
        "demo_runs": 0,
        "orders_submitted": 0,
        "migration": {
            "current_database_revision": database_revision,
            "inspected_target": "20260811_0015",
            "applied": False,
            "safe_additive_upgrade": True,
            "downgrade_drops_only_v4_tables": True,
            "controlled_command": ".\\.venv\\Scripts\\alembic.exe upgrade 20260811_0015",
        },
        "next_step": (
            "Collect synchronized spot/perp/funding/account-cost shadow telemetry; do not run Demo."
            if not any_supported
            else "Perform an independent offline replication before considering any short Demo canary."
        ),
    }
    write_json(output_dir / "recommendation.json", recommendation)
    recommendation_md = "\n".join((
        "# ByBot V5 Alpha Lab recommendation",
        "",
        f"Status: **{status}**",
        "",
        f"Strongest supported family: **{strongest}**.",
        "",
        "Carry cannot be evaluated honestly from the historical database. The required synchronized spot/perpetual, mark/index, exact funding-event, predicted-funding, account-fee and maker telemetry is absent. Required carry CSV files contain headers only and zero invented rows.",
        "",
        f"Long-horizon development-selected horizon: {momentum['selected_on_development_only']}; promotion passed: {momentum_gate['passed']}.",
        f"Relative-value development-selected horizon: {relative['selected_on_development_only']}; promotion passed: {relative_gate['passed']}.",
        "",
        "No Demo run, exchange mutation, database mutation or order submission occurred.",
        "",
        f"Next: {recommendation['next_step']}",
    )) + "\n"
    (output_dir / "recommendation.md").write_text(recommendation_md, encoding="utf-8")
    return recommendation
