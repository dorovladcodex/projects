from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
from typing import Any


TERMINAL_STATES = {
    "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE", "DEMO_CLOSED_AFTER_INTERRUPTION",
    "DEMO_CLOSED_EXTERNALLY", "DEMO_FAILED_FLAT_VERIFIED", "DEMO_ORDER_CANCELLED",
    "DEMO_NOT_SUBMITTED",
}


class V2ReportGenerator:
    def __init__(self, repository: Any, base_directory: str) -> None:
        self.repository = repository
        self.base_directory = Path(base_directory)

    def generate(self, run_id: str) -> dict[str, Any]:
        rows = self.repository.v2_report_rows(run_id)
        signals = rows["signals"]
        rejections = rows["rejections"]
        executions = rows["executions"]
        incidents = rows["incidents"]
        runtime = rows.get("runtime") or {}
        # Repository query is run-scoped; assert again to prevent report leakage.
        for collection in (signals, rejections, executions, incidents):
            if any(str(item.get("run_id")) != run_id for item in collection):
                raise ValueError("report data contains a different run_id")
        directory = self.base_directory / run_id
        directory.mkdir(parents=True, exist_ok=True)
        trades = [item for item in executions if str(item.get("state")) in TERMINAL_STATES]
        news_items = [
            item.get("payload") or {} for item in incidents
            if item.get("event_type") == "V2_NEWS_ITEM_AUDIT"
        ]
        news_decisions = [
            item.get("payload") or {} for item in incidents
            if item.get("event_type") == "V2_NEWS_DECISION_AUDIT"
        ]
        operational_incidents = [
            item for item in incidents
            if not str(item.get("event_type") or "").startswith("V2_NEWS_")
        ]
        trade_rows = [_trade_row(item) for item in trades]
        generated_at = datetime.now(timezone.utc)
        summary = self._summary(
            run_id, signals, rejections, executions, trades,
            operational_incidents, runtime, trade_rows, generated_at,
        )
        decision_reasons = Counter(
            str(item.get("llm_decision_reason") or item.get("rejection_reason") or "unknown")
            for item in news_decisions
        )
        summary["news_funnel_reasons"] = dict(decision_reasons)
        funnel = summary.get("news_funnel") or {}
        funnel["missing_keyword_rejections"] = max(
            int(funnel.get("missing_keyword_rejections") or 0),
            int(decision_reasons.get("missing_keywords") or 0),
        )
        funnel["low_importance_rejections"] = max(
            int(funnel.get("low_importance_rejections") or 0),
            int(decision_reasons.get("low_importance") or 0),
        )
        summary["news_funnel"] = funnel
        _write_json(directory / "summary.json", summary)
        _write_json(directory / "incidents.json", operational_incidents)
        _write_json(directory / "news_sources.json", {
            "metrics": runtime.get("news_source_metrics") or {},
            "health": runtime.get("news_source_health") or {},
        })
        _write_csv(directory / "signals.csv", [_signal_row(item) for item in signals])
        _write_csv(directory / "rejections.csv", [_rejection_row(item) for item in rejections])
        _write_csv(directory / "trades.csv", trade_rows)
        _write_csv(directory / "news_items.csv", [_news_item_row(item) for item in news_items])
        _write_csv(
            directory / "news_decisions.csv",
            [_news_decision_row(item) for item in news_decisions],
        )
        return {**summary, "artifact_directory": str(directory.resolve())}

    @staticmethod
    def _summary(
        run_id: str, signals: list[dict[str, Any]], rejections: list[dict[str, Any]],
        executions: list[dict[str, Any]], trades: list[dict[str, Any]],
        incidents: list[dict[str, Any]], runtime: dict[str, Any],
        trade_rows: list[dict[str, Any]], generated_at: datetime,
    ) -> dict[str, Any]:
        pnl = [_d(item.get("realized_exchange_pnl")) for item in trades if item.get("realized_exchange_pnl") is not None]
        wins = [value for value in pnl if value > 0]
        losses = [value for value in pnl if value < 0]
        fees = sum((_d(item.get("exchange_fees")) for item in trades), Decimal("0"))
        gross = sum(pnl, Decimal("0")) + fees
        net = sum(pnl, Decimal("0"))
        profit_factor = (
            sum(wins, Decimal("0")) / abs(sum(losses, Decimal("0")))
            if losses else None
        )
        holdings = []
        for item in trades:
            if item.get("created_at") and item.get("updated_at"):
                holdings.append((datetime.fromisoformat(item["updated_at"]) - datetime.fromisoformat(item["created_at"])).total_seconds())
        by_strategy = Counter(str(item.get("strategy_name") or "unknown") for item in signals)
        by_symbol = Counter(str(item.get("symbol") or "unknown") for item in signals)
        order_strategy = Counter(str(item.get("strategy_name") or "unknown") for item in executions)
        order_symbol = Counter(str(item.get("symbol") or "unknown") for item in executions)
        exit_reasons = Counter(_canonical_attribution(item) for item in trades)
        failures = [item for item in incidents if item.get("event_type") == "V2_CYCLE_FAILURE"]
        total_failures = sum(
            int((item.get("payload") or {}).get("occurrence_count") or 1)
            for item in failures
        )
        fingerprints = {
            str((item.get("payload") or {}).get("traceback_fingerprint") or item.get("id"))
            for item in failures
        }
        symbol_metrics = runtime.get("symbol_cycle_metrics") or {}
        accepted_symbols = list(runtime.get("accepted_symbols") or [])
        symbols_attempted = sorted(
            symbol for symbol, metric in symbol_metrics.items()
            if int(metric.get("cycles_attempted") or 0) > 0
        )
        symbols_successful = sorted(
            symbol for symbol, metric in symbol_metrics.items()
            if int(metric.get("cycles_succeeded") or 0) > 0
        )
        symbols_failed = sorted(
            symbol for symbol, metric in symbol_metrics.items()
            if int(metric.get("cycles_failed") or 0) > 0
        )
        never_processed = sorted(set(accepted_symbols) - set(symbols_successful))
        evaluations = dict(runtime.get("strategy_evaluation_counts") or {})
        enabled = list(runtime.get("enabled_strategies") or [])
        never_evaluated = sorted(
            strategy for strategy in enabled if int(evaluations.get(strategy, 0)) == 0
        )
        blockers: list[str] = []
        if total_failures:
            blockers.append("unhandled V2_CYCLE_FAILURE exists")
        repeat_limit = int(runtime.get("cycle_failure_repeat_limit") or 3)
        if any(
            int((item.get("payload") or {}).get("occurrence_count") or 1) > repeat_limit
            and not bool((item.get("payload") or {}).get("transient"))
            for item in failures
        ):
            blockers.append("deterministic cycle failure repeat limit exceeded")
        if never_processed:
            blockers.append("accepted symbols were never successfully processed")
        if never_evaluated:
            blockers.append("enabled strategies never entered their evaluation loop")
        if int(runtime.get("strategy_ineligible_evaluations") or 0):
            blockers.append("strategy evaluated an ineligible symbol")
        if any(
            not (item.get("payload") or {}).get("message")
            or not (item.get("payload") or {}).get("processing_stage")
            for item in failures
        ):
            blockers.append("cycle failure reports are not explanatory")
        news_metrics = dict(runtime.get("news_metrics") or {})
        signal_metrics = dict(runtime.get("signal_metrics") or {})
        signal_metrics.update({
            "strategy_evaluations": sum(int(value) for value in evaluations.values()),
            "orders_submitted": sum(bool(item.get("order_id")) for item in executions),
            "orders_filled": sum(_d(item.get("accepted_quantity")) > 0 for item in executions),
            "completed_trades": len(trades),
        })
        signal_metrics.setdefault("raw_candidates", len(signals))
        signal_metrics.setdefault("deduplicated_candidates", 0)
        signal_metrics.setdefault(
            "threshold_passes",
            sum(_d(item.get("distance_to_threshold")) >= 0 for item in signals),
        )
        signal_metrics.setdefault(
            "risk_rejections",
            sum(str(item.get("state")) == "EXECUTION_BLOCKED" for item in signals),
        )
        signal_metrics.setdefault("portfolio_rejections", 0)
        signal_metrics.setdefault("pre_execution_admissions", 0)
        signal_metrics.setdefault("persistence_rejections", 0)
        signal_metrics.setdefault("execution_policy_rejections", 0)
        signal_metrics.setdefault("cooldown_rejections", 0)
        signal_metrics.setdefault(
            "admitted_signals", sum(bool(item.get("admitted")) for item in signals)
        )
        unattributed = [
            item for item in trades
            if _canonical_attribution(item) == "unattributed_external_close"
        ]
        analytics_blockers = (
            ["completed trades lack canonical exit attribution"] if unattributed else []
        )
        analytics_warnings: list[str] = []
        stale_runtime = runtime.get("stale_metrics") or {}
        if int(stale_runtime.get("position_stale_observations") or 0):
            analytics_warnings.append("owned-position stale feature observations occurred")
        if any(row.get("latency_validation_errors") for row in trade_rows):
            analytics_blockers.append("trade latency contains incompatible or impossible timestamps")
        source_metrics = runtime.get("news_source_metrics") or {}
        news_funnel = _news_funnel_totals(source_metrics)
        analytics_blockers.extend(_news_funnel_blockers(source_metrics))
        model_usage = runtime.get("last_news_model_usage")
        primary_model = runtime.get("news_primary_model")
        fallback_model = runtime.get("news_fallback_model")
        last_model = runtime.get("last_news_model_used")
        if last_model is not None and last_model not in {primary_model, fallback_model}:
            analytics_blockers.append("last_news_model_used is not a configured news model")
        if model_usage is not None and model_usage.get("model") != last_model:
            analytics_blockers.append("news model usage fields refer to different events")
        liquidation_metrics, liquidation_blockers = _liquidation_metrics_at(
            runtime.get("liquidation_metrics") or {}, generated_at
        )
        analytics_blockers.extend(liquidation_blockers)
        source_liquidation_age = (
            ((runtime.get("stale_metrics") or {}).get("data_age_seconds_by_source") or {})
            .get("liquidations", {})
            .get("latest_message_age")
        )
        if source_liquidation_age is not None and (
            not math.isfinite(float(source_liquidation_age))
            or float(source_liquidation_age) < 0
        ):
            analytics_blockers.append("liquidation source age is invalid")
        analytics_blockers = list(dict.fromkeys(analytics_blockers))
        analytics_result = (
            "FAIL" if analytics_blockers
            else "PASS_WITH_WARNINGS" if analytics_warnings
            else "PASS"
        )
        persistence_rejections = int(signal_metrics.get("persistence_rejections") or 0)
        if persistence_rejections:
            blockers.append("execution compatibility persistence failed")
            blockers = list(dict.fromkeys(blockers))
        return {
            "run_id": run_id, "generated_at": generated_at.isoformat(),
            "functional_result": "FAIL" if blockers else "PASS",
            "functional_blockers": blockers,
            "analytics_result": analytics_result,
            "analytics_blockers": analytics_blockers,
            "analytics_warnings": analytics_warnings,
            "total_cycle_failures": total_failures,
            "unique_cycle_failure_fingerprints": len(fingerprints),
            "symbols_attempted": symbols_attempted,
            "symbols_successful": symbols_successful,
            "symbols_failed": symbols_failed,
            "symbols_never_processed": never_processed,
            "symbol_cycle_metrics": symbol_metrics,
            "strategy_evaluation_counts": evaluations,
            "enabled_strategies_never_evaluated": never_evaluated,
            "strategy_not_applicable_counts": runtime.get("strategy_not_applicable_counts") or {},
            "signal_metrics": signal_metrics,
            "strategy_evaluations": signal_metrics.get("strategy_evaluations", 0),
            "raw_candidates": signal_metrics.get("raw_candidates", len(signals)),
            "deduplicated_candidates": signal_metrics.get("deduplicated_candidates", 0),
            "threshold_passes": signal_metrics.get("threshold_passes", 0),
            "risk_rejections": signal_metrics.get("risk_rejections", 0),
            "portfolio_rejections": signal_metrics.get("portfolio_rejections", 0),
            "pre_execution_admissions": signal_metrics.get(
                "pre_execution_admissions", 0
            ),
            "persistence_rejections": persistence_rejections,
            "execution_policy_rejections": signal_metrics.get(
                "execution_policy_rejections", 0
            ),
            "cooldown_rejections": signal_metrics.get("cooldown_rejections", 0),
            "admitted_signals": signal_metrics.get(
                "admitted_signals", sum(bool(item.get("admitted")) for item in signals)
            ),
            "orders_submitted": signal_metrics["orders_submitted"],
            "orders_filled": signal_metrics["orders_filled"],
            "news_source_health": runtime.get("news_source_health") or {},
            "raw_news_feed_items_received": int(
                news_metrics.get("raw_news_feed_items_received")
                or news_metrics.get("items_received") or 0
            ),
            "unique_news_items_discovered": int(
                news_metrics.get("unique_news_items_discovered") or 0
            ),
            "news_items_classified": int(news_metrics.get("llm_classifications") or 0),
            "news_funnel_reasons": news_metrics.get("news_funnel_reasons") or {},
            "news_source_metrics": source_metrics,
            "news_funnel": news_funnel,
            "news_primary_model": primary_model,
            "news_fallback_model": fallback_model,
            "last_news_model_usage": model_usage,
            "last_news_model_used": last_model,
            "last_news_fallback_used": runtime.get("last_news_fallback_used"),
            "signals_by_strategy": dict(by_strategy), "signals_by_symbol": dict(by_symbol),
            "rejections_by_reason": dict(Counter(str(item.get("rejection_reason") or item.get("reason") or "unknown") for item in rejections)),
            "orders_by_strategy": dict(order_strategy), "orders_by_symbol": dict(order_symbol),
            "completed_trades": len(trades),
            "open_positions": sum(str(item.get("state")) == "DEMO_POSITION_OPEN" for item in executions),
            "long_short_split": dict(Counter(str(item.get("side") or "unknown") for item in executions)),
            "win_rate": str(Decimal(len(wins)) / Decimal(len(pnl))) if pnl else None,
            "average_win": str(sum(wins, Decimal("0")) / len(wins)) if wins else None,
            "average_loss": str(sum(losses, Decimal("0")) / len(losses)) if losses else None,
            "expectancy_after_fees": str(net / len(pnl)) if pnl else None,
            "profit_factor": str(profit_factor) if profit_factor is not None else None,
            "gross_pnl": str(gross), "total_fees": str(fees), "net_pnl": str(net),
            "maximum_drawdown": str(_max_drawdown(pnl)),
            "maximum_concurrent_positions": _maximum_concurrency(executions),
            "maximum_concurrent_notional": str(_maximum_notional(executions)),
            "average_holding_seconds": sum(holdings) / len(holdings) if holdings else None,
            "exit_counts": dict(exit_reasons),
            "unattributed_exit_count": len(unattributed),
            "attribution_failure_details": [
                {
                    "execution_id": item.get("id"),
                    "symbol": item.get("symbol"),
                    "reason": item.get("attribution_failure_reason"),
                }
                for item in unattributed
            ],
            "latency_ms": _latency_summaries(trade_rows),
            "reconciliation_incidents": sum("RECONCIL" in str(item.get("event_type") or "") for item in incidents),
            "websocket_reconnects": sum(int((item.get("payload") or {}).get("reconnects") or 0) for item in incidents if item.get("event_type") == "WEBSOCKET_RECONNECT"),
            "critical_stale_data_incidents": int(
                (runtime.get("stale_metrics") or {}).get("critical_stale_data_incidents") or 0
            ),
            "stale_feature_rejections": int(
                (runtime.get("stale_metrics") or {}).get("stale_feature_rejections") or 0
            ),
            "position_stale_observations": int(
                (runtime.get("stale_metrics") or {}).get("position_stale_observations") or 0
            ),
            "stale_rejections_by_source": (
                runtime.get("stale_metrics") or {}
            ).get("stale_rejections_by_source") or {},
            "stale_rejections_by_symbol": (
                runtime.get("stale_metrics") or {}
            ).get("stale_rejections_by_symbol") or {},
            "stale_rejections_by_strategy": (
                runtime.get("stale_metrics") or {}
            ).get("stale_rejections_by_strategy") or {},
            "data_age_seconds_by_source": (
                runtime.get("stale_metrics") or {}
            ).get("data_age_seconds_by_source") or {},
            "liquidation_eligibility": liquidation_metrics,
            "top_rejection_reasons": Counter(str(item.get("rejection_reason") or item.get("reason") or "unknown") for item in rejections).most_common(10),
        }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or ["run_id"])
        writer.writeheader()
        writer.writerows(rows)


def _signal_row(item: dict[str, Any]) -> dict[str, Any]:
    scores = item.get("score_components") or {}
    return {
        "run_id": item.get("run_id"), "candidate_id": item.get("id"),
        "created_at": item.get("created_at"), "strategy": item.get("strategy_name"),
        "strategy_version": item.get("strategy_version"), "symbol": item.get("symbol"),
        "side": item.get("side"), "market_regime": item.get("market_regime"),
        "raw_score": item.get("raw_strategy_score"), "final_score": scores.get("final_score"),
        "threshold": item.get("threshold"), "distance_to_threshold": item.get("distance_to_threshold"),
        "estimated_edge_bps": item.get("estimated_edge_bps"),
        "entry_reason": item.get("entry_reason"), "state": item.get("state"),
    }


def _rejection_row(item: dict[str, Any]) -> dict[str, Any]:
    scores = item.get("score_components") or {}
    return {
        "run_id": item.get("run_id"), "candidate_id": item.get("id") or item.get("candidate_id"),
        "created_at": item.get("created_at"), "strategy": item.get("strategy_name"),
        "symbol": item.get("symbol"), "side": item.get("side"),
        "raw_score": item.get("raw_strategy_score"),
        "final_score": scores.get("final_score"), "threshold": item.get("threshold"),
        "distance_to_threshold": item.get("distance_to_threshold"),
        "estimated_edge_bps": item.get("estimated_edge_bps"),
        "state": item.get("state"),
        "rejection_reason": item.get("rejection_reason") or item.get("reason"),
    }


def _news_item_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "news_id": item.get("news_id"), "source": item.get("source"),
        "title": item.get("title"), "url": item.get("url"),
        "published_at": item.get("published_at"), "received_at": item.get("received_at"),
        "deduplication_status": item.get("deduplication_status"),
        "duplicate_scope": item.get("duplicate_scope"),
        "normalized_identity": item.get("normalized_identity"),
        "normalized_identities": json.dumps(item.get("normalized_identities") or []),
        "first_seen_news_id": item.get("first_seen_news_id"),
        "first_seen_run_id": item.get("first_seen_run_id"),
        "poll_time": item.get("poll_time"),
        "detected_entities": json.dumps(item.get("detected_entities") or []),
        "mapped_symbols": json.dumps(item.get("mapped_symbols") or []),
        "market_wide": item.get("market_wide"),
    }


def _news_decision_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "news_id": item.get("news_id"),
        "aggregator_filter_decision": item.get("aggregator_filter_decision"),
        "deterministic_filter_decision": item.get("deterministic_filter_decision"),
        "classifier_prefilter_decision": item.get("classifier_prefilter_decision"),
        "llm_decision_reason": item.get("llm_decision_reason"),
        "funnel_stage": item.get("funnel_stage"),
        "llm_used": item.get("llm_used"), "model": item.get("model"),
        "fallback_used": item.get("fallback_used"),
        "fallback_reason": item.get("fallback_reason"),
        "request_attempt_number": item.get("request_attempt_number"),
        "failure_category": item.get("failure_category"),
        "classification_status": item.get("classification_status"),
        "sentiment": item.get("sentiment"), "importance": item.get("importance"),
        "urgency": item.get("urgency"), "confidence": item.get("confidence"),
        "mapped_symbols": json.dumps(item.get("mapped_symbols") or []),
        "market_confirmation_result": json.dumps(item.get("market_confirmation_result") or {}),
        "candidate_ids": json.dumps(item.get("candidate_ids") or []),
        "final_decision": item.get("final_decision"),
        "rejection_reason": item.get("rejection_reason"),
    }


def _trade_row(item: dict[str, Any]) -> dict[str, Any]:
    attribution = _canonical_attribution(item)
    entry_fills = item.get("fills") or []
    exit_fills = item.get("close_fills") or []
    errors: list[str] = []
    diagnostics: list[str] = []
    for name, value in (item.get("execution_stage_durations_ms") or {}).items():
        if float(value) < 0:
            errors.append(f"negative_latency:{name}")
    for name in ("order_submit_to_first_fill_ms", "ack_to_first_fill_ms"):
        if item.get(name) is not None and float(item[name]) < 0:
            errors.append(f"negative_latency:{name}")
    local_fill = item.get("local_fill_received_at") or next(
        (
            row.get("local_received_at")
            for row in entry_fills
            if row.get("local_received_at")
        ),
        None,
    )
    local_submit = item.get("local_submit_started_at") or item.get("order_submitted_at")
    local_ack = item.get("local_ack_received_at") or item.get("order_acknowledged_at")
    exchange_fill = item.get("exchange_fill_at") or item.get("first_fill_at")
    fill_before_ack = bool(item.get("fill_before_ack"))
    if local_fill and local_ack and _raw_latency_ms(local_ack, local_fill) < 0:
        fill_before_ack = True
        diagnostics.append("fill_received_before_ack")
    if exchange_fill and not local_fill:
        diagnostics.append("local_fill_receipt_unavailable")
    ack_to_fill = None
    if not fill_before_ack:
        if item.get("ack_to_first_fill_ms") is not None:
            ack_to_fill = item.get("ack_to_first_fill_ms")
        else:
            ack_to_fill = _validated_latency_ms(
                local_ack, local_fill, errors, "ack_to_first_fill"
            )
            if ack_to_fill is not None:
                diagnostics.append("wall_clock_fallback:ack_to_first_fill")
    if fill_before_ack:
        diagnostics.append("fill_before_ack_supported")
    position_after_fill_receipt = _ordered_local_latency_ms(
        local_fill,
        item.get("position_confirmed_at"),
        errors,
        diagnostics,
        "first_fill_to_position_confirmed",
        negative_code="position_confirmed_before_fill_receipt",
    )
    submit_to_fill = item.get("order_submit_to_first_fill_ms")
    if submit_to_fill is None:
        submit_to_fill = _validated_latency_ms(
            local_submit, local_fill, errors, "order_submit_to_first_fill"
        )
        if submit_to_fill is not None:
            diagnostics.append("wall_clock_fallback:order_submit_to_first_fill")
    row = {
        "run_id": item.get("run_id"), "execution_id": item.get("id"),
        "candidate_id": item.get("candidate_id"), "strategy": item.get("strategy_name"),
        "symbol": item.get("symbol"), "side": item.get("side"),
        "leverage": item.get("leverage"), "quantity": item.get("accepted_quantity"),
        "entry_order_id": item.get("order_id"), "exit_order_id": item.get("close_order_id"),
        "entry_execution_ids": json.dumps([row.get("execution_id") for row in entry_fills]),
        "exit_execution_ids": json.dumps([row.get("execution_id") for row in exit_fills]),
        "entry_price": item.get("average_fill_price"), "exit_price": item.get("average_close_price"),
        "entry_slippage": item.get("entry_slippage"), "exit_slippage": item.get("exit_slippage"),
        "fees": item.get("exchange_fees"), "tp": item.get("take_profit"),
        "sl": item.get("stop_loss"), "mfe": item.get("maximum_favorable_excursion"),
        "mae": item.get("maximum_adverse_excursion"),
        "exit_attribution": attribution, "exit_reason": attribution,
        "exit_attribution_evidence": json.dumps(
            item.get("exit_attribution_evidence") or {}, ensure_ascii=False
        ),
        "attribution_failure_reason": item.get("attribution_failure_reason"),
        "gross_pnl": str(
            _d(item.get("realized_exchange_pnl")) + _d(item.get("exchange_fees"))
        ),
        "net_pnl": item.get("realized_exchange_pnl"), "opened_at": item.get("created_at"),
        "closed_at": item.get("closed_at") or item.get("updated_at"),
        "signal_to_order_latency_ms": _validated_latency_ms(item.get("signal_created_at"), local_submit, errors, "signal_to_order"),
        "order_to_fill_latency_ms": submit_to_fill,
        "candidate_created_at": item.get("signal_created_at"),
        "candidate_persisted_at": item.get("candidate_persisted_at"),
        "reservation_requested_at": item.get("reservation_requested_at"),
        "reservation_created_at": item.get("reservation_created_at"),
        "risk_evaluation_started_at": item.get("risk_evaluation_started_at"),
        "risk_approved_at": item.get("risk_approved_at"),
        "execution_dispatched_at": item.get("execution_dispatched_at"),
        "execution_task_received_at": item.get("execution_task_received_at"),
        "order_submit_started_at": local_submit,
        "order_acknowledged_at": local_ack,
        "exchange_order_created_at": item.get("exchange_order_created_at"),
        "exchange_fill_at": exchange_fill,
        "local_submit_started_at": local_submit,
        "local_ack_received_at": local_ack,
        "local_fill_received_at": local_fill,
        "fill_before_ack": fill_before_ack,
        "first_fill_at": exchange_fill,
        "position_confirmed_at": item.get("position_confirmed_at"),
        "protection_confirmed_at": item.get("protection_confirmed_at"),
        "candidate_created_to_persisted_ms": _validated_latency_ms(item.get("signal_created_at"), item.get("candidate_persisted_at"), errors, "candidate_created_to_persisted"),
        "candidate_persisted_to_reservation_ms": _validated_latency_ms(item.get("candidate_persisted_at"), item.get("reservation_created_at"), errors, "candidate_persisted_to_reservation"),
        "reservation_to_risk_approved_ms": _validated_latency_ms(item.get("reservation_created_at"), item.get("risk_approved_at"), errors, "reservation_to_risk_approved"),
        "risk_approved_to_execution_dispatch_ms": _validated_latency_ms(item.get("risk_approved_at"), item.get("execution_dispatched_at"), errors, "risk_approved_to_execution_dispatch"),
        "execution_dispatch_to_order_submit_ms": _validated_latency_ms(item.get("execution_dispatched_at"), local_submit, errors, "execution_dispatch_to_order_submit"),
        "order_submit_to_ack_ms": (item.get("execution_stage_durations_ms") or {}).get("exchange_submit"),
        "order_submit_to_first_fill_ms": submit_to_fill,
        "ack_to_first_fill_ms": ack_to_fill,
        "first_fill_to_position_confirmed_ms": position_after_fill_receipt,
        "position_confirmed_to_protection_confirmed_ms": _validated_latency_ms(item.get("position_confirmed_at"), item.get("protection_confirmed_at"), errors, "position_confirmed_to_protection_confirmed"),
        "total_signal_to_order_ms": _validated_latency_ms(item.get("signal_created_at"), local_submit, errors, "total_signal_to_order"),
        "total_signal_to_fill_ms": _validated_latency_ms(item.get("signal_created_at"), local_fill, errors, "total_signal_to_fill"),
        "latency_validation_errors": errors,
        "latency_diagnostic_codes": [],
        "exchange_order_to_fill_ms": _validated_latency_ms(
            item.get("exchange_order_created_at"), exchange_fill, errors,
            "exchange_order_to_fill",
        ),
        "execution_stage_durations_ms": json.dumps(
            item.get("execution_stage_durations_ms") or {}, sort_keys=True
        ),
    }
    for stage in (
        "ownership_check", "reconciliation_check", "account_verification",
        "position_query", "open_orders_query", "instrument_metadata",
        "leverage_setup", "quantity_normalization", "protection_plan",
        "database_execution_state",
    ):
        row[f"{stage}_started_at"] = item.get(f"{stage}_started_at")
        row[f"{stage}_completed_at"] = item.get(f"{stage}_completed_at")
        row[f"{stage}_ms"] = (item.get("execution_stage_durations_ms") or {}).get(stage)
        if row[f"{stage}_ms"] is None:
            row[f"{stage}_ms"] = _validated_latency_ms(
                item.get(f"{stage}_started_at"), item.get(f"{stage}_completed_at"),
                errors, stage,
            )
            if row[f"{stage}_ms"] is not None:
                diagnostics.append(f"wall_clock_fallback:{stage}")
    row["latency_diagnostic_codes"] = list(dict.fromkeys(diagnostics))
    return row


def _d(value: object) -> Decimal:
    return Decimal(str(value or "0"))


def _max_drawdown(pnl: list[Decimal]) -> Decimal:
    equity = peak = Decimal("0"); maximum = Decimal("0")
    for value in pnl:
        equity += value; peak = max(peak, equity); maximum = max(maximum, peak - equity)
    return maximum


def _maximum_concurrency(executions: list[dict[str, Any]]) -> int:
    events: list[tuple[datetime, int]] = []
    for item in executions:
        if item.get("created_at"):
            events.append((datetime.fromisoformat(item["created_at"]), 1))
        if item.get("updated_at") and str(item.get("state")) in TERMINAL_STATES:
            events.append((datetime.fromisoformat(item["updated_at"]), -1))
    current = maximum = 0
    for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
        current += delta; maximum = max(maximum, current)
    return maximum


def _maximum_notional(executions: list[dict[str, Any]]) -> Decimal:
    return max((_d(item.get("accepted_quantity")) * _d(item.get("average_fill_price")) for item in executions), default=Decimal("0"))


def _latency_ms(start: object, finish: object) -> float | None:
    if not start or not finish:
        return None
    value = _raw_latency_ms(start, finish)
    return value if value >= 0 else None


def _raw_latency_ms(start: object, finish: object) -> float:
    return (_utc_datetime(finish) - _utc_datetime(start)).total_seconds() * 1000


def _utc_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _validated_latency_ms(
    start: object,
    finish: object,
    errors: list[str],
    label: str,
) -> float | None:
    if not start or not finish:
        return None
    try:
        value = _raw_latency_ms(start, finish)
    except (TypeError, ValueError, OverflowError):
        errors.append(f"invalid_utc_timestamp:{label}")
        return None
    if value < 0:
        errors.append(f"negative_latency:{label}")
        return None
    return value


def _ordered_local_latency_ms(
    start: object,
    finish: object,
    errors: list[str],
    diagnostics: list[str],
    label: str,
    *,
    negative_code: str,
) -> float | None:
    if not start or not finish:
        return None
    try:
        value = _raw_latency_ms(start, finish)
    except (TypeError, ValueError, OverflowError):
        errors.append(f"invalid_utc_timestamp:{label}")
        return None
    if value < 0:
        diagnostics.append(negative_code)
        return None
    return value


def _canonical_attribution(item: dict[str, Any]) -> str:
    value = str(item.get("exit_attribution") or item.get("close_reason") or "").strip().casefold()
    aliases = {
        "invalidated_setup": "strategy_exit", "runner_cleanup": "forced_cleanup",
        "protection_failure": "forced_cleanup", "exchange_close": "reconciliation_close",
        "exchange_generated_tp": "take_profit",
        "exchange_generated_sl": "stop_loss",
        "external_close": "manual_external_close",
    }
    value = aliases.get(value, value)
    allowed = {
        "take_profit", "stop_loss", "strategy_exit", "stale_signal",
        "maximum_holding_time", "reconciliation_close", "manual_external_close",
        "forced_cleanup",
        "unattributed_external_close",
    }
    return value if value in allowed else "unattributed_external_close"


def _latency_summaries(trades: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = {
        "signal_to_order": "total_signal_to_order_ms",
        "order_to_ack": "order_submit_to_ack_ms",
        "ack_to_fill": "ack_to_first_fill_ms",
        "signal_to_fill": "total_signal_to_fill_ms",
    }

    def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
        result: dict[str, dict[str, float | int | None]] = {}
        for name, field in metrics.items():
            values = sorted(
                float(row[field]) for row in rows
                if row.get(field) is not None and float(row[field]) >= 0
            )
            result[name] = {
                "count": len(values),
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "maximum": max(values) if values else None,
            }
        return result

    strategies = sorted({str(item.get("strategy") or "unknown") for item in trades})
    symbols = sorted({str(item.get("symbol") or "unknown") for item in trades})
    return {
        "total_run": summarize(trades),
        "by_strategy": {
            value: summarize([row for row in trades if str(row.get("strategy") or "unknown") == value])
            for value in strategies
        },
        "by_symbol": {
            value: summarize([row for row in trades if str(row.get("symbol") or "unknown") == value])
            for value in symbols
        },
    }


def _news_funnel_blockers(source_metrics: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    for source, values in source_metrics.items():
        raw = int(values.get("raw_feed_items_received") or 0)
        invalid = int(values.get("invalid_feed_items") or 0)
        duplicate_poll = int(values.get("duplicate_within_poll") or 0)
        duplicate_run = int(values.get("duplicate_within_run") or 0)
        duplicate_previous = int(values.get("duplicate_from_previous_run") or 0)
        unique = int(values.get("unique_items_discovered") or 0)
        accepted = int(values.get("deterministic_filter_accepts") or 0)
        rejected = int(values.get("deterministic_filter_rejections") or 0)
        if raw != invalid + duplicate_poll + duplicate_run + duplicate_previous + unique:
            blockers.append(f"news funnel raw invariant failed: {source}")
        if int(values.get("duplicate_items_seen") or 0) != (
            duplicate_poll + duplicate_run + duplicate_previous
        ):
            blockers.append(f"news duplicate scope invariant failed: {source}")
        if unique != accepted + rejected:
            blockers.append(f"news funnel deterministic invariant failed: {source}")
    return blockers


def _news_funnel_totals(source_metrics: dict[str, Any]) -> dict[str, int]:
    mapping = {
        "received": "raw_feed_items_received",
        "unique": "unique_items_discovered",
        "fresh": "fresh_items",
        "symbol_matched": "symbol_matched_items",
        "deterministic_accepted": "deterministic_filter_accepts",
        "llm_requested": "items_sent_to_llm",
        "classified": "classified_items",
        "trade_eligible": "trade_eligible_items",
        "candidate": "candidates_generated",
        "admitted": "candidates_admitted",
        "cache_hits": "llm_cache_hits",
        "budget_rejections": "llm_budget_rejections",
        "circuit_breaker_rejections": "llm_circuit_breaker_rejections",
        "classifier_failures": "classifier_failures",
        "previous_run_duplicates": "duplicate_from_previous_run",
        "missing_keyword_rejections": "skipped_missing_keywords",
        "low_importance_rejections": "skipped_low_importance",
    }
    return {
        output: sum(int(values.get(source) or 0) for values in source_metrics.values())
        for output, source in mapping.items()
    }


def _liquidation_metrics_at(
    raw: dict[str, Any], generated_at: datetime,
) -> tuple[dict[str, Any], list[str]]:
    metrics = dict(raw)
    source_rows = (
        raw.get("liquidation_eligibility_by_symbol")
        or raw.get("by_symbol")
        or {}
    )
    by_symbol: dict[str, dict[str, Any]] = {}
    timestamps: list[datetime] = []
    blockers: list[str] = []
    raw_age_reference = raw.get("age_calculated_at")
    if raw_age_reference:
        blockers.extend(
            _liquidation_age_blockers(
                raw, datetime.fromisoformat(str(raw_age_reference))
            )
        )
    for symbol, value in source_rows.items():
        row = dict(value)
        stamp_text = row.get("last_valid_timestamp")
        stamp = datetime.fromisoformat(str(stamp_text)) if stamp_text else None
        if stamp is None:
            if row.get("current_age_seconds") is not None:
                blockers.append(f"liquidation null timestamp has non-null age: {symbol}")
            healthy_zero = _healthy_zero_liquidation_row(row)
            if row.get("state") != "INELIGIBLE" and not healthy_zero:
                blockers.append(f"liquidation uninitialized symbol is not ineligible: {symbol}")
            if not healthy_zero and row.get("not_applicable_reason") not in {
                "liquidation_feed_never_initialized", "liquidation_feed_unavailable",
            }:
                blockers.append(f"liquidation uninitialized reason is invalid: {symbol}")
        calculated_age = (
            max(0.0, (generated_at - stamp).total_seconds()) if stamp else None
        )
        # The runtime age may have been calculated before final report generation.
        # Rebase it to the one authoritative report timestamp, then validate the
        # final pair below. Generic source health timestamps are intentionally
        # outside this per-symbol calculation.
        row["current_age_seconds"] = calculated_age
        by_symbol[symbol] = row
        if stamp:
            timestamps.append(stamp)
    metrics["liquidation_eligibility_by_symbol"] = by_symbol
    metrics["most_recent_valid_liquidation_timestamp"] = (
        max(timestamps).isoformat() if timestamps else None
    )
    metrics["most_recent_age_seconds"] = (
        max(0.0, (generated_at - max(timestamps)).total_seconds())
        if timestamps else None
    )
    metrics["oldest_valid_liquidation_timestamp"] = (
        min(timestamps).isoformat() if timestamps else None
    )
    metrics["maximum_age_seconds"] = (
        max(0.0, (generated_at - min(timestamps)).total_seconds())
        if timestamps else None
    )
    metrics["eligible_symbol_count"] = sum(
        row.get("state") == "ELIGIBLE" for row in by_symbol.values()
    )
    metrics["ineligible_symbol_count"] = sum(
        row.get("state") != "ELIGIBLE" for row in by_symbol.values()
    )
    metrics.pop("current_liquidation_data_age_seconds", None)
    metrics.pop("last_valid_liquidation_timestamp", None)
    metrics.pop("by_symbol", None)
    metrics["age_calculated_at"] = generated_at.isoformat()
    blockers.extend(_liquidation_age_blockers(metrics, generated_at))
    return metrics, list(dict.fromkeys(blockers))


def _liquidation_age_blockers(
    metrics: dict[str, Any], generated_at: datetime,
    *, tolerance_seconds: float = 0.010,
) -> list[str]:
    """Validate only timestamp/age pairs from the same liquidation scope."""
    blockers: list[str] = []
    rows = metrics.get("liquidation_eligibility_by_symbol") or {}
    for symbol, row in rows.items():
        stamp_text = row.get("last_valid_timestamp")
        age = row.get("current_age_seconds")
        if not stamp_text:
            if age is not None:
                blockers.append(f"liquidation null timestamp has non-null age: {symbol}")
            healthy_zero = _healthy_zero_liquidation_row(row)
            if row.get("state") != "INELIGIBLE" and not healthy_zero:
                blockers.append(f"liquidation uninitialized symbol is not ineligible: {symbol}")
            if not healthy_zero and row.get("not_applicable_reason") not in {
                "liquidation_feed_never_initialized", "liquidation_feed_unavailable",
            }:
                blockers.append(f"liquidation uninitialized reason is invalid: {symbol}")
            continue
        stamp = datetime.fromisoformat(str(stamp_text))
        expected = (generated_at - stamp).total_seconds()
        if (
            age is None or not math.isfinite(float(age)) or expected < 0
            or abs(float(age) - expected) > tolerance_seconds
        ):
            blockers.append(f"liquidation timestamp/age mismatch: {symbol}")

    aggregate_pairs = (
        (
            "most_recent_valid_liquidation_timestamp", "most_recent_age_seconds",
            "liquidation most-recent timestamp/age mismatch",
        ),
        (
            "oldest_valid_liquidation_timestamp", "maximum_age_seconds",
            "liquidation oldest/maximum timestamp/age mismatch",
        ),
    )
    for timestamp_field, age_field, message in aggregate_pairs:
        stamp_text = metrics.get(timestamp_field)
        age = metrics.get(age_field)
        if stamp_text is None:
            if age is not None:
                blockers.append(message)
            continue
        stamp = datetime.fromisoformat(str(stamp_text))
        expected = (generated_at - stamp).total_seconds()
        if (
            age is None or not math.isfinite(float(age)) or expected < 0
            or abs(float(age) - expected) > tolerance_seconds
        ):
            blockers.append(message)
    return blockers


def _healthy_zero_liquidation_row(row: dict[str, Any]) -> bool:
    return (
        row.get("state") == "ELIGIBLE"
        and row.get("connection_state") == "CONNECTED"
        and row.get("subscription_state") == "SUBSCRIBED"
        and int(row.get("rolling_event_count") or 0) == 0
        and _d(row.get("rolling_liquidation_notional")) == 0
        and row.get("not_applicable_reason") in {None, ""}
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    index = min(len(values) - 1, max(0, int((len(values) - 1) * percentile)))
    return values[index]
