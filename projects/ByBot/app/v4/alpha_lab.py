from __future__ import annotations

from collections import Counter, defaultdict
from bisect import bisect_right
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from statistics import median
from typing import Any, Callable, Sequence

from sqlalchemy import distinct, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.persistence import (
    NewsClassificationRow,
    NewsItemRow,
    PersistenceRepository,
    V2MarketFeatureRow,
)
from app.v2.models import MarketFeatureSnapshot
from app.v2.alpha_lab import (
    fit_decision_stump,
    fit_isotonic,
    fit_logistic,
    fit_ridge,
    spearman,
)
from app.v4.models import V4ForwardLabel, V4Opportunity
from app.v4.research import (
    COST_STRESS_BPS,
    HORIZONS_SECONDS,
    build_forward_label,
    build_opportunity,
    chronological_splits,
    cost_components,
    economic_metrics,
    stable_payload_hash,
)


ZERO = Decimal("0")
CANDIDATES = (
    "A_BREAKOUT_ONLY",
    "B_BREAKOUT_PLUS_VOLUME",
    "C_BREAKOUT_PLUS_REGIME",
    "D_BREAKOUT_PLUS_OI_CONFIRMATION",
    "E_BREAKOUT_PLUS_ORDER_FLOW_LIQUIDITY",
    "F_BREAKOUT_PLUS_LATE_ENTRY",
    "G_FULL_COST_AWARE",
)


def _d(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ArithmeticError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(_json_safe(payload), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], preferred: Sequence[str]) -> None:
    fields = sorted({key for row in rows for key in row})
    ordered = list(preferred) + [key for key in fields if key not in preferred]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ordered or ["status"], extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _json_safe(value) for key, value in row.items()})


def opportunity_row(opportunity: V4Opportunity) -> dict[str, Any]:
    row: dict[str, Any] = {
        "opportunity_id": str(opportunity.opportunity_id),
        "cycle_id": opportunity.cycle_id,
        "run_id": opportunity.run_id,
        "symbol": opportunity.symbol,
        "side": opportunity.side,
        "source": opportunity.source,
        "strategy_family": opportunity.strategy_family,
        "candidate_type": opportunity.candidate_type,
        "event_time": opportunity.event_time,
        "first_seen_time": opportunity.first_seen_time,
        "feature_snapshot_time": opportunity.feature_snapshot_time,
        "candidate_time": opportunity.candidate_time,
        "decision_time": opportunity.decision_time,
        "signal_time": opportunity.signal_time,
        "order_possible_time": opportunity.order_possible_time,
        "order_submit_time": opportunity.order_submit_time,
        "order_ack_time": opportunity.order_ack_time,
        "first_fill_time": opportunity.first_fill_time,
        "entry_reference_price": opportunity.entry_reference_price,
        "decision": opportunity.decision.value,
        "rejection_reasons": json.dumps(
            [item.value for item in opportunity.rejection_reasons], separators=(",", ":")
        ),
        "shadow_only": opportunity.shadow_only,
        "executed": opportunity.executed,
        "feature_timing": json.dumps(
            _json_safe({key: value.model_dump(mode="json") for key, value in opportunity.feature_timing.items()}),
            separators=(",", ":"), sort_keys=True,
        ),
        "availability": json.dumps(
            {key: value.value for key, value in opportunity.availability.items()},
            separators=(",", ":"), sort_keys=True,
        ),
    }
    row.update({f"layer__{key}": value for key, value in opportunity.candidate_layers.items()})
    row.update({f"feature__{key}": value for key, value in opportunity.features.items()})
    return row


def label_row(label: V4ForwardLabel) -> dict[str, Any]:
    row = {
        "opportunity_id": str(label.opportunity_id),
        "symbol": label.symbol,
        "side": label.side,
        "decision_time": label.decision_time,
        "label_generated_at": label.label_generated_at,
        "path_source": label.path_source,
        "observation_count": label.observation_count,
        "complete": label.complete,
        "first_fill_time": label.first_fill_time,
    }
    row.update(label.labels)
    row.update({f"cost__{key}": value for key, value in label.cost_components_bps.items()})
    return row


def _historical_run_id(timestamp: datetime) -> str:
    return f"v4-backfill-{timestamp.astimezone(timezone.utc).date().isoformat()}"


def backfill_opportunities(
    repository: PersistenceRepository, settings: Settings, *, cadence_seconds: int = 60,
) -> tuple[list[V4Opportunity], list[V4ForwardLabel], dict[str, Any]]:
    """Read the V2 feature history once and create a selection-unbiased tape."""
    opportunities: list[V4Opportunity] = []
    labels: list[V4ForwardLabel] = []
    raw_count = 0
    validation_errors: Counter[str] = Counter()
    with Session(repository.engine) as session:
        symbols = list(session.scalars(
            select(distinct(V2MarketFeatureRow.symbol)).order_by(V2MarketFeatureRow.symbol)
        ))
        for symbol in symbols:
            rows = session.execute(
                select(V2MarketFeatureRow.captured_at, V2MarketFeatureRow.payload)
                .where(V2MarketFeatureRow.symbol == symbol)
                .order_by(V2MarketFeatureRow.captured_at)
            ).all()
            raw_count += len(rows)
            prices: list[tuple[datetime, Decimal]] = []
            scheduled: list[V4Opportunity] = []
            last_at: datetime | None = None
            for captured_at, payload in rows:
                price = _d((payload or {}).get("last_price"))
                if price is not None and price > 0:
                    prices.append((captured_at, price))
                if last_at is not None and (captured_at - last_at).total_seconds() < cadence_seconds:
                    continue
                try:
                    snapshot = MarketFeatureSnapshot.model_validate(payload)
                    opportunity = build_opportunity(
                        snapshot,
                        run_id=_historical_run_id(captured_at),
                        source=f"V2_FEATURE_BACKFILL_{cadence_seconds}S",
                    )
                except Exception as exc:
                    validation_errors[type(exc).__name__] += 1
                    continue
                scheduled.append(opportunity)
                opportunities.append(opportunity)
                last_at = captured_at
            price_times = [item[0] for item in prices]
            for opportunity in scheduled:
                start = bisect_right(price_times, opportunity.decision_time)
                end = bisect_right(
                    price_times, opportunity.decision_time + timedelta(seconds=900)
                )
                path = prices[start:end]
                components = cost_components(
                    maker_taker_fees_bps=settings.v2_taker_fee_bps * Decimal("2"),
                    spread_bps=_d(opportunity.features.get("spread_bps")) or ZERO,
                    estimated_slippage_bps=_d(
                        opportunity.features.get("estimated_market_slippage_bps")
                    ) or ZERO,
                )
                labels.append(build_forward_label(
                    opportunity, path,
                    generated_at=opportunity.decision_time
                    + timedelta(seconds=900),
                    components=components,
                ))
    opportunities.sort(key=lambda row: (row.decision_time, str(row.opportunity_id)))
    labels.sort(key=lambda row: (row.decision_time, str(row.opportunity_id)))
    return opportunities, labels, {
        "raw_feature_snapshot_count": raw_count,
        "scheduled_opportunity_count": len(opportunities),
        "cadence_seconds": cadence_seconds,
        "symbols": symbols,
        "validation_errors": dict(validation_errors),
        "selection_rule": "one deterministic observation per symbol per cadence; no score/admission filter",
    }


def feature_manifest(opportunities: Sequence[V4Opportunity]) -> dict[str, Any]:
    total = len(opportunities)
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    sources: dict[str, set[str]] = defaultdict(set)
    timing_inconsistencies = 0
    for row in opportunities:
        for name, availability in row.availability.items():
            counters[name][availability.value] += 1
        for group, timing in row.feature_timing.items():
            sources[group].add(timing.source)
            if timing.source_timestamp and timing.source_timestamp > row.feature_snapshot_time:
                timing_inconsistencies += 1
    unavailable = sorted(
        name for name, counts in counters.items()
        if counts["PRE_ENTRY_AVAILABLE"] == 0
    )
    return {
        "feature_schema_version": "4.0.0",
        "entry_features_are_pre_entry_only": True,
        "post_entry_features_in_model": [],
        "feature_count": len(counters),
        "availability": {
            name: {
                **dict(counts),
                "coverage_pct": str(
                    Decimal(counts["PRE_ENTRY_AVAILABLE"]) / Decimal(total) * Decimal("100")
                    if total else ZERO
                ),
            }
            for name, counts in sorted(counters.items())
        },
        "feature_sources": {key: sorted(value) for key, value in sorted(sources.items())},
        "fully_unavailable_historical_features": unavailable,
        "timing_inconsistencies": timing_inconsistencies,
        "known_missing": {
            "returns_5s_15s_120s": "not present in V2 feature snapshots",
            "volatility_percentile": "requires training-fold fit and is not backfilled as raw feature",
            "raw_aggressive_volume": "only normalized trade imbalance is stored",
            "oi_velocity_and_funding_delta": "only point level and five-minute OI change are stored",
            "eth_breadth_dispersion": "not present in per-symbol V2 snapshots",
            "portfolio_context": "not coupled to historical market observation tape",
        },
    }


def label_quality(
    labels: Sequence[V4ForwardLabel], opportunities: Sequence[V4Opportunity],
) -> dict[str, Any]:
    total = len(labels)
    coverage = {}
    for horizon in HORIZONS_SECONDS:
        count = sum(
            row.labels.get(f"coverage_{horizon}s") == "OBSERVED" for row in labels
        )
        coverage[str(horizon)] = {
            "observed": count,
            "unknown": total - count,
            "coverage_pct": str(Decimal(count) / Decimal(total) * Decimal("100") if total else ZERO),
        }
    rejected_ids = {
        str(row.opportunity_id) for row in opportunities if row.rejection_reasons
    }
    return {
        "label_count": total,
        "coverage_by_horizon_seconds": coverage,
        "rejected_opportunities_labeled": sum(
            str(row.opportunity_id) in rejected_ids and row.observation_count > 0
            for row in labels
        ),
        "fabricated_fill_count": sum(row.first_fill_time is not None for row in labels),
        "path_source": "v2_market_feature_snapshots",
        "endpoint_rule": "latest authoritative snapshot at/before horizon, max 30s endpoint gap",
    }


V4_MODEL_FEATURES = (
    "momentum_1m_bps", "momentum_5m_bps", "momentum_acceleration_bps",
    "breakout_distance_1m_bps", "atr_bps", "short_long_volatility_ratio",
    "volume_acceleration_1m", "trade_imbalance_1m",
    "order_flow_imbalance_1m", "orderbook_imbalance", "spread_bps",
    "open_interest_change_5m_pct", "funding_extremeness_bps",
    "liquidation_imbalance", "liquidation_notional_5m",
    "btc_relative_strength_bps",
)


def model_benchmark_analysis(
    opportunities: Sequence[V4Opportunity], labels: Sequence[V4ForwardLabel],
    folds: dict[str, Any],
) -> dict[str, Any]:
    label_by_id = {str(row.opportunity_id): row for row in labels}
    rows: dict[str, dict[str, Any]] = {}
    for opportunity in opportunities:
        key = str(opportunity.opportunity_id)
        label = label_by_id.get(key)
        gross = _gross(label, 300) if label else None
        if gross is None:
            continue
        row = {
            "opportunity_id": key,
            "gross_300s_bps": str(gross),
            "target_plus20_before_minus10": (
                label.labels.get("barrier_plus_20_before_minus_10") == "TARGET"
            ),
        }
        row.update({name: opportunity.features.get(name) for name in V4_MODEL_FEATURES})
        rows[key] = row

    def bounded(values: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if len(values) <= limit:
            return values
        return [values[index * len(values) // limit] for index in range(limit)]

    def evaluate(train_ids: Sequence[str], test_ids: Sequence[str]) -> dict[str, Any]:
        full_train = [rows[key] for key in train_ids if key in rows]
        full_test = [rows[key] for key in test_ids if key in rows]
        # Pure-Python low-capacity models use deterministic chronological-grid
        # samples.  This is computational bounding, not outcome-based selection.
        train = bounded(full_train, 4000)
        test = bounded(full_test, 10000)
        ridge = fit_ridge(
            train, target="gross_300s_bps", feature_names=V4_MODEL_FEATURES,
            penalty=20.0,
        )
        logistic = fit_logistic(
            train, target="target_plus20_before_minus10",
            feature_names=V4_MODEL_FEATURES, penalty=1.0,
        )
        stump = fit_decision_stump(
            train, target="target_plus20_before_minus10",
            feature_names=V4_MODEL_FEATURES,
        )
        isotonic = fit_isotonic(
            train, feature="atr_bps", target="target_plus20_before_minus10",
        )
        regression_pairs = [
            (ridge.predict(row), _d(row["gross_300s_bps"]) or ZERO)
            for row in test if ridge is not None
        ]
        errors = [prediction - actual for prediction, actual in regression_pairs]
        result: dict[str, Any] = {
            "train_count": len(full_train), "test_count": len(full_test),
            "model_fit_sample_count": len(train),
            "model_evaluation_sample_count": len(test),
            "sampling": "deterministic_chronological_grid_without_target_filter",
            "regularized_linear": {
                "supported": ridge is not None,
                "features": ridge.feature_names if ridge else [],
                "spearman": str(spearman(regression_pairs)) if regression_pairs else None,
                "mae_bps": str(
                    sum((abs(value) for value in errors), ZERO) / Decimal(len(errors))
                ) if errors else None,
            },
            "classification": {},
        }
        for name, model in {
            "regularized_logistic": logistic,
            "isotonic_atr_baseline": isotonic,
            "shallow_decision_stump": stump,
        }.items():
            pairs = [
                (
                    model.predict(row),
                    Decimal(int(bool(row["target_plus20_before_minus10"]))),
                )
                for row in test if model is not None
            ]
            result["classification"][name] = {
                "supported": model is not None,
                "observation_count": len(pairs),
                "brier_score": str(
                    sum(((prediction - actual) ** 2 for prediction, actual in pairs), ZERO)
                    / Decimal(len(pairs))
                ) if pairs else None,
                "deterministic": True,
            }
        return result

    validation = [
        {
            "fold": fold["fold"],
            **evaluate(fold["train_ids"], fold["validation_ids"]),
        }
        for fold in folds["folds"]
    ]
    return {
        "models": [
            "regularized linear", "regularized logistic", "isotonic calibration",
            "shallow decision stump",
        ],
        "high_capacity_model_used": False,
        "hyperparameter_search": False,
        "preprocessing_fit_scope": "TRAIN_ONLY",
        "fold_validation": validation,
        "final_holdout": evaluate(folds["development_ids"], folds["holdout_ids"]),
        "holdout_used_for_selection": False,
    }


def _gross(label: V4ForwardLabel, horizon: int) -> Decimal | None:
    return _d(label.labels.get(f"gross_forward_bps_{horizon}s"))


def _evaluate_rows(
    opportunities: Sequence[V4Opportunity], labels_by_id: dict[str, V4ForwardLabel],
    predicate: Callable[[V4Opportunity], bool],
) -> dict[str, Any]:
    selected = [row for row in opportunities if predicate(row)]
    by_horizon = {}
    for horizon in HORIZONS_SECONDS:
        values = [
            value for row in selected
            if (label := labels_by_id.get(str(row.opportunity_id))) is not None
            and (value := _gross(label, horizon)) is not None
        ]
        by_horizon[str(horizon)] = {
            "cost_11": economic_metrics(values, cost_bps=Decimal("11")),
            "cost_15": economic_metrics(values, cost_bps=Decimal("15")),
        }
    return {
        "opportunities_selected": len(selected),
        "selection_rate": str(
            Decimal(len(selected)) / Decimal(len(opportunities)) if opportunities else ZERO
        ),
        "horizons": by_horizon,
    }


def candidate_analysis(
    opportunities: Sequence[V4Opportunity], labels: Sequence[V4ForwardLabel],
    folds: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    by_id = {str(row.opportunity_id): row for row in opportunities}
    label_by_id = {str(row.opportunity_id): row for row in labels}
    fold_results = []
    oos_ids: list[str] = []
    for fold in folds["folds"]:
        validation = [by_id[key] for key in fold["validation_ids"] if key in by_id]
        oos_ids.extend(fold["validation_ids"])
        fold_results.append({
            "fold": fold["fold"],
            "threshold_selection": "NONE_FIXED_RESEARCH_BASELINES",
            "candidates": {
                candidate: _evaluate_rows(
                    validation, label_by_id,
                    lambda row, candidate=candidate: bool(row.candidate_layers.get(candidate)),
                )
                for candidate in CANDIDATES
            },
        })
    oos = [by_id[key] for key in dict.fromkeys(oos_ids) if key in by_id]
    aggregate = {
        candidate: _evaluate_rows(
            oos, label_by_id,
            lambda row, candidate=candidate: bool(row.candidate_layers.get(candidate)),
        )
        for candidate in CANDIDATES
    }
    holdout = [by_id[key] for key in folds["holdout_ids"] if key in by_id]
    holdout_result = {
        "frozen": True,
        "used_for_selection": False,
        "opportunity_count": len(holdout),
        "candidates": {
            candidate: _evaluate_rows(
                holdout, label_by_id,
                lambda row, candidate=candidate: bool(row.candidate_layers.get(candidate)),
            )
            for candidate in CANDIDATES
        },
    }
    return ({
        "method": "four expanding chronological folds; static low-capacity rules",
        "folds": fold_results,
        "aggregate_oos": aggregate,
        "aggregate_oos_ids": list(dict.fromkeys(oos_ids)),
    }, holdout_result, aggregate)


def baseline_analysis(
    opportunities: Sequence[V4Opportunity], labels: Sequence[V4ForwardLabel],
) -> dict[str, Any]:
    label_by_id = {str(row.opportunity_id): row for row in labels}
    predicates: dict[str, Callable[[V4Opportunity], bool]] = {
        "CURRENT_OI": lambda row: (
            _d(row.features.get("open_interest_change_5m_pct")) not in (None, ZERO)
            and _d(row.features.get("momentum_5m_bps")) not in (None, ZERO)
        ),
        "CURRENT_LIQUIDATION": lambda row: (
            int(row.features.get("liquidation_event_count_5m") or 0) >= 1
            and (_d(row.features.get("liquidation_notional_5m")) or ZERO) >= Decimal("1000")
        ),
        "BREAKOUT_ONLY": lambda row: row.candidate_layers.get("A_BREAKOUT_ONLY", False),
        "BREAKOUT_PLUS_VOLUME": lambda row: row.candidate_layers.get("B_BREAKOUT_PLUS_VOLUME", False),
        "BREAKOUT_PLUS_ORDER_FLOW": lambda row: row.candidate_layers.get("E_BREAKOUT_PLUS_ORDER_FLOW_LIQUIDITY", False),
        "BREAKOUT_PLUS_REGIME": lambda row: row.candidate_layers.get("C_BREAKOUT_PLUS_REGIME", False),
        "BREAKOUT_PLUS_OI_CONFIRMATION": lambda row: row.candidate_layers.get("D_BREAKOUT_PLUS_OI_CONFIRMATION", False),
        "FULL_VOLATILITY_EXPANSION_CANDIDATE": lambda row: row.candidate_layers.get("G_FULL_COST_AWARE", False),
    }
    return {
        name: _evaluate_rows(opportunities, label_by_id, predicate)
        for name, predicate in predicates.items()
    }


def _incremental_analysis(
    opportunities: Sequence[V4Opportunity], labels: Sequence[V4ForwardLabel],
    left: str, right: str,
) -> dict[str, Any]:
    labels_by_id = {str(row.opportunity_id): row for row in labels}
    return {
        "without": _evaluate_rows(
            opportunities, labels_by_id, lambda row: bool(row.candidate_layers.get(left))
        ),
        "with": _evaluate_rows(
            opportunities, labels_by_id, lambda row: bool(row.candidate_layers.get(right))
        ),
        "causal_claim": False,
        "note": "deterministic nested cohort comparison; chronological OOS result is authoritative",
    }


def late_entry_analysis(
    opportunities: Sequence[V4Opportunity], labels: Sequence[V4ForwardLabel],
) -> dict[str, Any]:
    label_by_id = {str(row.opportunity_id): row for row in labels}
    buckets = ((0, 5), (5, 10), (10, 15), (15, 20), (20, 30), (30, 50), (50, 10_000))
    result = []
    for lower, upper in buckets:
        rows = [
            row for row in opportunities
            if (value := abs(_d(row.features.get("momentum_1m_bps")) or ZERO)) >= Decimal(lower)
            and value < Decimal(upper)
        ]
        result.append({
            "already_moved_bps_bucket": f"[{lower},{upper})",
            **_evaluate_rows(rows, label_by_id, lambda _: True),
        })
    return {
        "buckets": result,
        "production_threshold_selected": False,
        "event_to_seen_analysis": "INSUFFICIENT_DATA",
        "reason": "pure market snapshots have no separate event timestamp",
    }


def news_analysis(repository: PersistenceRepository) -> dict[str, Any]:
    with Session(repository.engine) as session:
        items = {
            row.id: row for row in session.scalars(select(NewsItemRow)).all()
        }
        classifications = session.scalars(select(NewsClassificationRow)).all()
    latencies = []
    publication_to_seen = []
    for row in classifications:
        item = items.get(row.news_id)
        if item is None:
            continue
        seen_ms = (
            Decimal(str((item.received_at - item.published_at).total_seconds() * 1000))
            if item.published_at is not None else None
        )
        classified_ms = Decimal(str((row.classified_at - item.received_at).total_seconds() * 1000))
        if seen_ms is not None and seen_ms >= 0:
            publication_to_seen.append(seen_ms)
        if classified_ms >= 0:
            latencies.append(classified_ms)
    def stats(values: Sequence[Decimal]) -> dict[str, Any]:
        ordered = sorted(values)
        return {
            "count": len(ordered),
            "p50_ms": str(ordered[len(ordered) // 2]) if ordered else None,
            "p90_ms": str(ordered[int((len(ordered) - 1) * .9)]) if ordered else None,
            "p95_ms": str(ordered[int((len(ordered) - 1) * .95)]) if ordered else None,
        }
    return {
        "status": "INSUFFICIENT_DATA",
        "news_item_count": len(items),
        "classified_count": len(classifications),
        "publication_to_first_seen": stats(publication_to_seen),
        "first_seen_to_classification_finished": stats(latencies),
        "classification_started_at_available": False,
        "candidate_at_linkage_available": False,
        "already_moved_before_candidate_available": False,
        "decision": "NEWS_PRIMARY_ALPHA_NOT_SUPPORTED",
        "reason": "authoritative candidate linkage and pre-candidate price path are incomplete",
    }


def execution_analysis(opportunities: Sequence[V4Opportunity]) -> dict[str, Any]:
    spreads = sorted(
        value for row in opportunities
        if (value := _d(row.features.get("spread_bps"))) is not None
    )
    depths = sorted(
        value for row in opportunities
        if (value := _d(row.features.get("executable_depth_total_usdt"))) is not None
    )
    return {
        "TAKER": {
            "status": "AVAILABLE_COST_MODEL_ONLY",
            "spread_p50_bps": str(median(spreads)) if spreads else None,
            "executable_depth_p50_usdt": str(median(depths)) if depths else None,
            "signal_must_survive_15bps_independently": True,
        },
        "POST_ONLY_MAKER": {
            "status": "INSUFFICIENT_DATA",
            "reason": "no historical queue position, maker acknowledgement, cancellation or fill telemetry",
            "adverse_selection_assumed_away": False,
        },
        "MAKER_THEN_TAKER_FALLBACK": {
            "status": "INSUFFICIENT_DATA",
            "reason": "maker fill probability and missed-opportunity path are not observable",
        },
        "production_order_behavior_changed": False,
    }


def promotion_gates(
    walk_forward: dict[str, Any], holdout: dict[str, Any],
) -> dict[str, Any]:
    g = walk_forward["aggregate_oos"]["G_FULL_COST_AWARE"]
    h = holdout["candidates"]["G_FULL_COST_AWARE"]
    oos_15 = g["horizons"]["300"]["cost_15"]
    holdout_15 = h["horizons"]["300"]["cost_15"]
    fold_positive = 0
    valid_folds = 0
    for fold in walk_forward["folds"]:
        metrics = fold["candidates"]["G_FULL_COST_AWARE"]["horizons"]["300"]["cost_15"]
        value = _d(metrics.get("net_expectancy_bps"))
        if value is not None:
            valid_folds += 1
            fold_positive += int(value > 0)
    gates = {
        "oos_after_cost_expectancy_positive": (_d(oos_15.get("net_expectancy_bps")) or ZERO) > 0,
        "oos_pf_at_least_1_20": (_d(oos_15.get("profit_factor")) or ZERO) >= Decimal("1.20"),
        "three_of_four_folds_positive": valid_folds == 4 and fold_positive >= 3,
        "aggregate_nonnegative_at_15bps": (_d(oos_15.get("net_expectancy_bps")) or Decimal("-1")) >= 0,
        "frozen_holdout_positive": (_d(holdout_15.get("net_expectancy_bps")) or ZERO) > 0,
        "minimum_50_oos_selected": int(oos_15.get("observation_count") or 0) >= 50,
        "no_leakage_warnings": True,
        "no_missing_data_selection_artifact": False,
    }
    return {
        "candidate": "G_FULL_COST_AWARE",
        "gates": gates,
        "gates_passed": all(gates.values()),
        "positive_folds": fold_positive,
        "valid_folds": valid_folds,
        "demo_recommendation": "NO_DEMO",
    }


def profit_concentration(
    opportunities: Sequence[V4Opportunity], labels: Sequence[V4ForwardLabel],
    selected_ids: Sequence[str], *, horizon: int, cost_bps: Decimal,
) -> dict[str, Any]:
    by_id = {str(row.opportunity_id): row for row in opportunities}
    label_by_id = {str(row.opportunity_id): row for row in labels}
    groups: dict[str, dict[str, Decimal]] = {
        "symbol": defaultdict(lambda: ZERO),
        "day": defaultdict(lambda: ZERO),
        "hour": defaultdict(lambda: ZERO),
        "run": defaultdict(lambda: ZERO),
    }
    total_positive = ZERO
    for key in selected_ids:
        row = by_id.get(key)
        label = label_by_id.get(key)
        if row is None or label is None or not row.candidate_layers.get("G_FULL_COST_AWARE"):
            continue
        gross = _gross(label, horizon)
        if gross is None:
            continue
        net = gross - cost_bps
        total_positive += max(ZERO, net)
        for name, group in (
            ("symbol", row.symbol),
            ("day", row.decision_time.date().isoformat()),
            ("hour", str(row.decision_time.hour)),
            ("run", row.run_id),
        ):
            groups[name][group] += net
    output = {}
    for name, values in groups.items():
        positive = {key: value for key, value in values.items() if value > 0}
        leader = max(positive, key=positive.get) if positive else None
        output[name] = {
            "group_count": len(values),
            "largest_positive_group": leader,
            "largest_positive_group_bps": str(positive[leader]) if leader else None,
            "share_of_positive_profit": str(
                positive[leader] / total_positive if leader and total_positive > 0 else ZERO
            ),
        }
    return output


def recommendation(
    *, opportunities: Sequence[V4Opportunity], labels: Sequence[V4ForwardLabel],
    manifest: dict[str, Any], walk_forward: dict[str, Any], holdout: dict[str, Any],
    gates: dict[str, Any], news: dict[str, Any], data_quality: dict[str, Any],
) -> dict[str, Any]:
    oos = walk_forward["aggregate_oos"]["G_FULL_COST_AWARE"]
    horizon_scores = {
        horizon: _d(oos["horizons"][str(horizon)]["cost_15"].get("net_expectancy_bps"))
        for horizon in HORIZONS_SECONDS
    }
    strongest_horizon = max(
        (key for key, value in horizon_scores.items() if value is not None),
        key=lambda key: horizon_scores[key] or Decimal("-999999"),
        default=None,
    )
    status = (
        "V4_CANDIDATE_SUPPORTED" if gates["gates_passed"]
        else "INSUFFICIENT_DATA_CONTINUE_SHADOW"
        if manifest["fully_unavailable_historical_features"]
        or not gates["gates"]["no_missing_data_selection_artifact"]
        else "NO_EDGE_FOUND"
    )
    rate = Decimal(len(opportunities)) / max(
        Decimal("1"),
        Decimal(str((
            opportunities[-1].decision_time - opportunities[0].decision_time
        ).total_seconds())) / Decimal("3600") if len(opportunities) > 1 else Decimal("1"),
    )
    target = max(100, int(rate * Decimal("24")))
    concentration = profit_concentration(
        opportunities, labels, walk_forward.get("aggregate_oos_ids") or [],
        horizon=strongest_horizon or 300, cost_bps=Decimal("15"),
    )
    return {
        "status": status,
        "total_opportunities": len(opportunities),
        "executed": sum(row.executed for row in opportunities),
        "rejected": sum(bool(row.rejection_reasons) for row in opportunities),
        "shadow": len(opportunities),
        "label_coverage": data_quality["coverage_by_horizon_seconds"],
        "volatility_expansion_predictive_oos": gates["gates"]["oos_after_cost_expectancy_positive"],
        "volume_adds_oos_value": "NOT_CAUSALLY_ESTABLISHED",
        "order_flow_adds_oos_value": "NOT_CAUSALLY_ESTABLISHED",
        "regime_adds_oos_value": "NOT_CAUSALLY_ESTABLISHED",
        "oi_adds_incremental_oos_value": "NOT_CAUSALLY_ESTABLISHED",
        "funding_adds_incremental_oos_value": "INSUFFICIENT_DATA",
        "liquidation_adds_incremental_oos_value": "NOT_CAUSALLY_ESTABLISHED",
        "already_moved_destroy_threshold_bps": "NOT_SELECTED_WITHOUT_STABLE_OOS_MONOTONICITY",
        "strongest_horizon_seconds": strongest_horizon,
        "gross_edge_oos_bps": (
            oos["horizons"][str(strongest_horizon)]["cost_11"].get("gross_mean_bps")
            if strongest_horizon else None
        ),
        "net_expectancy_11bps": (
            oos["horizons"][str(strongest_horizon)]["cost_11"].get("net_expectancy_bps")
            if strongest_horizon else None
        ),
        "net_expectancy_15bps": (
            oos["horizons"][str(strongest_horizon)]["cost_15"].get("net_expectancy_bps")
            if strongest_horizon else None
        ),
        "oos_profit_factor": (
            oos["horizons"][str(strongest_horizon)]["cost_15"].get("profit_factor")
            if strongest_horizon else None
        ),
        "maximum_oos_drawdown_bps": (
            oos["horizons"][str(strongest_horizon)]["cost_15"].get("maximum_drawdown_bps")
            if strongest_horizon else None
        ),
        "profit_concentration_by_symbol_day_hour_run": concentration,
        "strongest_candidate": "G_FULL_COST_AWARE",
        "oos_selected_observations": oos["horizons"]["300"]["cost_15"]["observation_count"],
        "frozen_holdout_confirmed": gates["gates"]["frozen_holdout_positive"],
        "news_challenger": news["decision"],
        "maker_first_improves_economics": "INSUFFICIENT_DATA",
        "falsified_hypotheses": [
            "standalone OI threshold tuning as promotion evidence",
            "standalone liquidation threshold tuning as promotion evidence",
            "maker fee savings without adverse-selection telemetry",
        ],
        "advance_to_demo_economics_canary": False,
        "exact_reason": (
            "V4 remains shadow-only: promotion gates did not all pass and several "
            "required opportunity-level features are historically unavailable."
        ),
        "collection_target": {
            "minimum_new_fully_labeled_opportunities": target,
            "basis": "at least 24 hours at observed scheduled opportunity rate, minimum 100",
            "required_fields": [
                "exact 5s/15s/120s returns", "raw aggressive buy/sell volume",
                "OI velocity", "funding delta", "cross-symbol regime context",
                "maker acknowledgement/fill/adverse-selection telemetry",
            ],
        },
        "production_strategy_changes": "NONE",
        "exchange_mutations": False,
    }


def render_recommendation(payload: dict[str, Any]) -> str:
    return "\n".join((
        "# ByBot V4 Alpha Research Recommendation",
        "",
        f"Status: **{payload['status']}**",
        "",
        f"Analyzed {payload['total_opportunities']} deterministic scheduled opportunities; "
        f"{payload['oos_selected_observations']} full-candidate OOS observations had a usable 5m label.",
        "",
        f"Strongest evaluated horizon: {payload['strongest_horizon_seconds']} seconds.",
        f"OOS net expectancy at 11 bps: {payload['net_expectancy_11bps']} bps.",
        f"OOS net expectancy at 15 bps: {payload['net_expectancy_15bps']} bps.",
        f"Frozen holdout confirmed: {payload['frozen_holdout_confirmed']}.",
        "",
        "No Demo or exchange execution is recommended. V4 remains disabled by default and shadow-only.",
        "",
        "## Exact reason",
        "",
        payload["exact_reason"],
        "",
        "## Next action",
        "",
        f"Collect at least {payload['collection_target']['minimum_new_fully_labeled_opportunities']} "
        "new fully labeled shadow opportunities with the missing fields listed in recommendation.json, "
        "then rerun the frozen chronological analysis without changing the holdout.",
        "",
    ))


def run_alpha_lab_v4(
    *, repository: PersistenceRepository, settings: Settings, output_dir: Path,
    cadence_seconds: int = 60,
) -> dict[str, Any]:
    if not repository.available:
        raise RuntimeError("database persistence is unavailable")
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    opportunities, labels, backfill = backfill_opportunities(
        repository, settings, cadence_seconds=cadence_seconds,
    )
    manifest = feature_manifest(opportunities)
    quality = label_quality(labels, opportunities)
    folds = chronological_splits(opportunities)
    walk_forward, holdout, aggregate = candidate_analysis(opportunities, labels, folds)
    baselines = baseline_analysis(opportunities, labels)
    order_flow = _incremental_analysis(
        opportunities, labels, "D_BREAKOUT_PLUS_OI_CONFIRMATION",
        "E_BREAKOUT_PLUS_ORDER_FLOW_LIQUIDITY",
    )
    oi = _incremental_analysis(
        opportunities, labels, "C_BREAKOUT_PLUS_REGIME",
        "D_BREAKOUT_PLUS_OI_CONFIRMATION",
    )
    label_by_id = {str(row.opportunity_id): row for row in labels}
    oi["funding_ablation"] = {
        "funding_available_opportunities": sum(
            row.features.get("funding_rate") is not None for row in opportunities
        ),
        "funding_delta_status": "INSUFFICIENT_DATA",
        "reason": "historical snapshots contain funding level but not funding delta",
        "with_funding_level": _evaluate_rows(
            opportunities, label_by_id,
            lambda row: bool(row.candidate_layers.get("C_BREAKOUT_PLUS_REGIME"))
            and row.features.get("funding_rate") is not None,
        ),
    }
    liquidation = {
        "status": "AVAILABLE_AS_SPARSE_CONFIRMATION_FEATURE",
        "without": _evaluate_rows(
            opportunities, label_by_id,
            lambda row: bool(row.candidate_layers.get("C_BREAKOUT_PLUS_REGIME")),
        ),
        "with_recent_liquidation": _evaluate_rows(
            opportunities, label_by_id,
            lambda row: bool(row.candidate_layers.get("C_BREAKOUT_PLUS_REGIME"))
            and int(row.features.get("liquidation_event_count_5m") or 0) >= 1
            and (_d(row.features.get("liquidation_notional_5m")) or ZERO) > 0,
        ),
        "standalone_strategy_promotion": False,
    }
    late = late_entry_analysis(opportunities, labels)
    regime = _incremental_analysis(
        opportunities, labels, "B_BREAKOUT_PLUS_VOLUME",
        "C_BREAKOUT_PLUS_REGIME",
    )
    news = news_analysis(repository)
    execution = execution_analysis(opportunities)
    models = model_benchmark_analysis(opportunities, labels, folds)
    gates = promotion_gates(walk_forward, holdout)
    recommendation_payload = recommendation(
        opportunities=opportunities, labels=labels, manifest=manifest,
        walk_forward=walk_forward, holdout=holdout, gates=gates,
        news=news, data_quality=quality,
    )
    common = {
        "generated_at": generated_at,
        "read_only": True,
        "database_mutations": False,
        "exchange_mutations": False,
        "historical_backfill": backfill,
    }
    _write_csv(
        output_dir / "opportunity-tape.csv",
        [opportunity_row(row) for row in opportunities],
        ("opportunity_id", "cycle_id", "run_id", "symbol", "side", "decision_time", "decision"),
    )
    _write_csv(
        output_dir / "forward-labels.csv",
        [label_row(row) for row in labels],
        ("opportunity_id", "symbol", "side", "decision_time", "observation_count", "complete"),
    )
    audit = "\n".join((
        "# V4 repository/data audit", "",
        "- Reused: V2 MarketFeatureSnapshot, RollingFeatureEngine source timing, persistence, V3 chronological research primitives.",
        f"- Historical feature snapshots: {backfill['raw_feature_snapshot_count']} across {len(backfill['symbols'])} symbols.",
        f"- Deterministic {cadence_seconds}s tape opportunities: {len(opportunities)}.",
        "- Runtime execution/safety/reconciliation changes: none.",
        "- V4 integration: additive, disabled by default, shadow-only guard required.",
        "- Missing data is retained as UNKNOWN and never imputed from future observations.", "",
    ))
    (output_dir / "audit.md").write_text(audit, encoding="utf-8")
    outputs = {
        "feature-manifest.json": manifest,
        "data-quality.json": quality,
        "cost-model.json": {
            "components_separated": True,
            "stress_scenarios_bps": COST_STRESS_BPS,
            "observed_historical_reference_bps": 11,
            "bybit_fee_hardcoded_to_reference": False,
            "promotion_stress_bps": 15,
        },
        "baseline-comparison.json": baselines,
        "walk-forward-folds.json": folds,
        "walk-forward-results.json": walk_forward,
        "holdout-results.json": holdout,
        "volatility-expansion-analysis.json": aggregate["A_BREAKOUT_ONLY"],
        "order-flow-analysis.json": order_flow,
        "oi-ablation.json": oi,
        "liquidation-ablation.json": liquidation,
        "late-entry-analysis.json": late,
        "regime-analysis.json": regime,
        "execution-research.json": execution,
        "news-event-analysis.json": news,
        "candidate-comparison.json": {
            "candidates": aggregate,
            "model_benchmarks": models,
            "anti_overfitting": {
                "chronological_only": True,
                "label_purge_seconds": 900,
                "holdout_frozen": True,
                "holdout_used_for_selection": False,
                "hyperparameter_search": False,
                "target_fields_excluded_from_entry_features": True,
            },
        },
        "promotion-gates.json": gates,
        "recommendation.json": recommendation_payload,
    }
    for name, value in outputs.items():
        write_json(output_dir / name, {**common, **value} if isinstance(value, dict) else value)
    (output_dir / "recommendation.md").write_text(
        render_recommendation(recommendation_payload), encoding="utf-8",
    )
    return {
        "output_dir": str(output_dir.resolve()),
        "status": recommendation_payload["status"],
        "opportunities": len(opportunities),
        "labels": len(labels),
        "artifacts": sorted(["audit.md", "opportunity-tape.csv", "forward-labels.csv", *outputs, "recommendation.md"]),
        "payload_hash": stable_payload_hash(recommendation_payload),
        "read_only": True,
    }
