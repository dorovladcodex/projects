from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import re
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from app.config import Settings
from app.models import Symbol
from app.news.service import NewsService
from app.v2.analytics import V2ReportGenerator
from app.v2.drain import V2DrainController, V2RunPhase
from app.v2.dependency_health import (
    DependencyHealthState,
    ExternalDependencyHealth,
    ExternalDependencySafetyError,
)
from app.v2.execution import V2ExecutionCoordinator
from app.v2.market import (
    BybitPublicWebSocketEngine, BybitRestMetricsPoller, RollingFeatureEngine,
)
from app.v2.logging import configure_v2_logging
from app.v2.models import NewsModelUsage, StrategyName, V2Incident, V2SignalCandidate
from app.v2.news import V2ExternalTrendService, V2NewsAggregator
from app.v2.outcomes import (
    DataIntegrityCriticalOutcome,
    NON_FAILURE_OUTCOMES,
    RuntimeOutcome,
    RuntimeOutcomeError,
    typed_outcome,
)
from app.v2.portfolio import PortfolioRiskService, correlation_group
from app.v2.research import CalibrationObservation, EmpiricalEdgeCalibrator
from app.v2.scoring import AdmissionContext, CommonScoringPipeline
from app.v2.strategies import MemeTrendContext, NewsStrategyContext, build_v2_strategies
from app.v2.universe import SymbolUniverseService


def protection_management_price_input(
    feature: Any,
) -> tuple[Decimal, datetime | None, datetime, str]:
    """Return a price and exchange timestamp from the same source."""
    return (
        feature.last_price,
        feature.source_timestamps.get("ticker"),
        feature.timestamp,
        "v2_feature_ticker_last_price",
    )


class V2Runtime:
    def __init__(
        self, settings: Settings, repository: Any,
        universe: SymbolUniverseService, features: RollingFeatureEngine,
        news_aggregator: V2NewsAggregator, v1_news_service: NewsService,
        portfolio: PortfolioRiskService, execution: V2ExecutionCoordinator,
        external_trends: V2ExternalTrendService | None = None,
        *, run_id: str,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.universe = universe
        self.features = features
        self.news_aggregator = news_aggregator
        self.news_aggregator.run_id = run_id
        self.v1_news_service = v1_news_service
        self.portfolio = portfolio
        self.execution = execution
        self.external_trends = external_trends
        self.run_id = run_id
        self.scoring = CommonScoringPipeline(settings)
        self.strategies = build_v2_strategies(settings)
        self.websocket = BybitPublicWebSocketEngine(settings, features)
        self.rest_metrics = BybitRestMetricsPoller(settings, features)
        self.reporter = V2ReportGenerator(repository, settings.v2_report_directory)
        self.logger = (
            configure_v2_logging(
                f"{settings.v2_report_directory}/{run_id}/events.jsonl",
                level=settings.log_level,
            )
            if settings.v2_enabled else logging.getLogger("bybot.v2.disabled")
        )
        self.candidates: list[V2SignalCandidate] = repository.load_v2_signal_candidates(run_id)
        self.edge_calibrator = EmpiricalEdgeCalibrator(
            settings.v2_min_calibration_samples
        )
        self._restore_calibration_history()
        run_state_loader = getattr(repository, "load_v2_run_state", None)
        restored_run = (
            run_state_loader(run_id) if callable(run_state_loader) else {}
        ) or {}
        restored = dict(restored_run.get("runtime") or {})
        if not restored:
            restored = (
                repository.load_v2_run_runtime(run_id)
                if hasattr(repository, "load_v2_run_runtime") else {}
            )
        self.started_at = _aware_utc_datetime(
            restored_run.get("started_at")
        ) if restored_run.get("started_at") else datetime.now(timezone.utc)
        self.last_cycle_at: datetime | None = None
        self.last_error: str | None = None
        self.stop_new_entries = False
        self.supervisor_entries_paused = bool(
            restored.get("supervisor_entries_paused", False)
        )
        self.supervisor_pause_reason: str | None = restored.get(
            "supervisor_pause_reason"
        )
        restored_finalization = dict(restored.get("run_finalization") or {})
        phase_order = {
            V2RunPhase.RUNNING.value: 0,
            V2RunPhase.DRAINING.value: 1,
            V2RunPhase.RECONCILING.value: 2,
            V2RunPhase.FINISHED.value: 3,
        }
        phase_values = [
            str(value)
            for value in (
                restored_finalization.get("phase"),
                restored_run.get("status"),
            )
            if value
        ]
        persisted_phase_value = max(
            phase_values or [V2RunPhase.RUNNING.value],
            key=lambda value: phase_order.get(value, -1),
        )
        try:
            persisted_phase = V2RunPhase(str(persisted_phase_value))
        except ValueError:
            persisted_phase = V2RunPhase.RUNNING
        nominal_value = (
            restored_finalization.get("nominal_end_at")
            if "nominal_end_at" in restored_finalization
            else settings.v2_run_nominal_end_at
        )
        restored_nominal = (
            _aware_utc_datetime(nominal_value) if nominal_value else None
        )
        drain_started_value = restored_finalization.get("drain_started_at")
        restored_drain_started = (
            _aware_utc_datetime(drain_started_value)
            if drain_started_value else None
        )
        self.drain = V2DrainController(
            restored_nominal,
            lead_seconds=settings.v2_drain_lead_seconds,
            timeout_seconds=settings.v2_drain_timeout_seconds,
            restored_phase=persisted_phase,
            drain_started_at=restored_drain_started,
        )
        self._persisted_phase = persisted_phase
        self._initial_restored_phase = self.drain.phase
        self._restored_runtime_updated_at = restored.get("updated_at")
        self._restoration_detected = bool(restored_run or restored)
        if self.drain.phase != V2RunPhase.RUNNING:
            self.stop_new_entries = True
        self.cycles = 0
        self._last_rest_poll_at: datetime | None = None
        self._execution_pools: dict[Symbol, ThreadPoolExecutor] = {}
        self.failure_circuit_breaker_active = bool(
            restored.get("failure_circuit_breaker_active", False)
        )
        if self.failure_circuit_breaker_active:
            self.stop_new_entries = True
        self.run_valid = bool(restored.get("run_valid", True))
        self.run_invalid_reasons: list[str] = list(
            restored.get("run_invalid_reasons") or []
        )
        self._terminalization_incident_ids: set[str] = set(
            restored.get("terminalization_incident_ids") or []
        )
        self.last_news_model_usage: NewsModelUsage | None = None
        restored_usage = restored.get("last_news_model_usage")
        if restored_usage:
            try:
                usage = NewsModelUsage.model_validate(restored_usage)
                if usage.model in {
                    settings.news_primary_model, settings.news_fallback_model
                }:
                    self.last_news_model_usage = usage
            except Exception:
                self.last_news_model_usage = None
        self.symbol_cycle_metrics: dict[str, dict[str, Any]] = dict(
            restored.get("symbol_cycle_metrics") or {}
        )
        self.strategy_evaluation_counts: dict[str, int] = dict(
            restored.get("strategy_evaluation_counts") or {}
        )
        self.strategy_not_applicable_counts: dict[str, int] = dict(
            restored.get("strategy_not_applicable_counts") or {}
        )
        self.signal_metrics: dict[str, Any] = dict(
            restored.get("signal_metrics") or {
                "strategy_evaluations": 0,
                "raw_candidates": 0,
                "deduplicated_candidates": 0,
                "threshold_passes": 0,
                "risk_rejections": 0,
                "portfolio_rejections": 0,
                "pre_execution_admissions": 0,
                "persistence_rejections": 0,
                "execution_policy_rejections": 0,
                "pre_submit_rejections": 0,
                "final_depth_rejections": 0,
                "pre_submit_rejections_by_code": {},
                "cooldown_rejections": 0,
                "admitted_signals": 0,
                "by_strategy": {},
                "by_symbol": {},
            }
        )
        for name, default in (
            ("pre_submit_rejections", 0),
            ("final_depth_rejections", 0),
            ("pre_submit_rejections_by_code", {}),
        ):
            self.signal_metrics.setdefault(name, default)
        self._candidate_signatures: dict[str, datetime] = {}
        for restored_candidate in self.candidates:
            self._candidate_signatures[_candidate_signature(restored_candidate)] = (
                restored_candidate.created_at
            )
        self._handled_pre_submit_rejections = {
            str(item.id)
            for item in self.candidates
            if item.pre_submit_rejection is not None
        }
        self._status_lock = RLock()
        self._status_snapshot: dict[str, Any] = {
            "enabled": settings.v2_enabled,
            "execution_environment": "BYBIT_DEMO",
            "run_id": run_id,
            "started_at": self.started_at.isoformat(),
            "status_snapshot_state": "INITIALIZING",
            "status_snapshot_at": datetime.now(timezone.utc).isoformat(),
        }
        self.status_request_count = 0
        self.status_request_failures = 0
        self.status_request_latency_ms = 0.0
        self.failure_occurrences: dict[str, int] = dict(
            restored.get("failure_occurrences") or {}
        )
        self.outcome_occurrences: dict[str, int] = dict(
            restored.get("outcome_occurrences") or {}
        )
        self.outcome_first_seen: dict[str, str] = dict(
            restored.get("outcome_first_seen") or {}
        )
        self.outcome_classification_counts: dict[str, int] = dict(
            restored.get("outcome_classification_counts") or {}
        )
        self.outcome_code_counts: dict[str, int] = dict(
            restored.get("outcome_code_counts") or {}
        )
        self.critical_classification_counts: dict[str, int] = dict(
            restored.get("critical_classification_counts") or {}
        )
        self.dependency_health = ExternalDependencyHealth(
            run_id=run_id,
            repository=repository,
            initial_backoff_seconds=(
                settings.v2_dependency_backoff_initial_seconds
            ),
            maximum_backoff_seconds=settings.v2_dependency_backoff_max_seconds,
            hard_outage_seconds=settings.v2_dependency_hard_outage_seconds,
            restored=restored.get("external_dependency_health"),
        )
        self.news_metrics: dict[str, int] = {
            "items_received": 0, "raw_news_feed_items_received": 0,
            "unique_news_items_discovered": 0, "items_deduplicated": 0,
            "items_rejected_by_deterministic_filter": 0,
            "items_sent_to_llm": 0, "llm_classifications": 0,
            "trade_eligible_items": 0, "mapped_assets": 0,
            "news_momentum_candidates_generated": 0,
            "news_momentum_candidates_admitted": 0,
            "news_funnel_reasons": {},
        }
        self.news_metrics.update(restored.get("news_metrics") or {})
        self.stale_metrics: dict[str, Any] = dict(restored.get("stale_metrics") or {
            "stale_feature_rejections": 0,
            "stale_rejections_by_source": {}, "stale_rejections_by_symbol": {},
            "stale_rejections_by_strategy": {},
            "position_stale_observations": 0,
        })
        self.stale_metrics.setdefault("position_stale_observations", 0)
        self.liquidation_metrics: dict[str, Any] = dict(
            restored.get("liquidation_metrics") or {
                "applicable_count": 0, "not_applicable_count": 0, "reasons": {},
                "last_valid_liquidation_timestamp": None,
                "current_liquidation_data_age_seconds": None,
                "by_symbol": {},
            }
        )
        self.liquidation_metrics.setdefault("by_symbol", {})
        restored_sources = restored.get("news_source_metrics") or {}
        for source, values in restored_sources.items():
            if source in self.news_aggregator.source_metrics:
                self.news_aggregator.source_metrics[source].update(values)
        self.news_aggregator.items_received = self.news_metrics.get("items_received", 0)
        self.news_aggregator.duplicate_count = self.news_metrics.get("items_deduplicated", 0)
        self.news_aggregator.items_rejected = self.news_metrics.get(
            "items_rejected_by_deterministic_filter", 0
        )
        restore_deduplication = getattr(
            self.news_aggregator, "restore_deduplication", None
        )
        if callable(restore_deduplication):
            restore_deduplication(self.v1_news_service.items)
        restore_run_audits = getattr(
            self.news_aggregator, "restore_current_run_audits", None
        )
        report_rows = getattr(self.repository, "v2_report_rows", None)
        if callable(restore_run_audits) and callable(report_rows):
            try:
                incidents = report_rows(self.run_id).get("incidents") or []
                restore_run_audits([
                    row.get("payload") or {}
                    for row in incidents
                    if row.get("event_type") == "V2_NEWS_ITEM_AUDIT"
                ])
            except Exception:
                # Persistence health is reported elsewhere; startup remains
                # safe and restored NewsItems still prevent reinsertion.
                pass
        self._refresh_status_snapshot()

    def start(self) -> None:
        if not self.settings.v2_enabled:
            return
        if not self.repository.begin_v2_run(self.run_id, self.started_at):
            raise RuntimeError("V2 run boundary could not be persisted")
        # The FastAPI lifespan validates the universe before private account
        # preflight. Reuse that exact snapshot instead of repeating all public
        # REST calls during the same startup. Direct/runtime-only callers still
        # receive a refresh when no validated snapshot exists.
        if self.universe.last_refresh_at is None:
            self.universe.refresh()
        if getattr(self, "_restoration_detected", False):
            self._restore_finalization_on_startup()
        self._refresh_status_snapshot()

    async def cycle(self) -> None:
        if not self.settings.v2_enabled:
            return
        self._update_drain_state()
        cycle_id = f"{self.run_id}:{self.cycles + 1}"
        cycle_failures_before = sum(self.failure_occurrences.values())
        now = datetime.now(timezone.utc)
        rest_poll_completed = False
        if (
            (self._last_rest_poll_at is None or (
            now - self._last_rest_poll_at
            ).total_seconds() >= self.settings.v2_rest_metrics_interval_seconds)
            and self.dependency_health.should_attempt(now)
        ):
            try:
                await asyncio.to_thread(
                    self.rest_metrics.poll, self.universe.accepted_symbols
                )
                rest_poll_completed = True
                self._last_rest_poll_at = now
                if (
                    self.dependency_health.state
                    != DependencyHealthState.HEALTHY
                ):
                    self.dependency_health.begin_recovery()
                    reconciliation = await asyncio.to_thread(
                        self.execution.demo_execution.reconcile
                    )
                    if reconciliation.get("status") not in {"OK", "DISABLED"}:
                        raise ExternalDependencySafetyError(
                            "authoritative Demo reconciliation did not recover"
                        )
                    active_count, protected = self._active_protection_state()
                    self.dependency_health.record_recovered(
                        dependency="bybit_rest",
                        active_position_count=active_count,
                        protection_confirmed=protected,
                        authoritative_reconciliation_succeeded=True,
                    )
            except Exception as exc:
                if not self._handle_dependency_failure(
                    exc, stage="rest_metrics", cycle_id=cycle_id
                ):
                    self._record_failure(
                        exc, stage="rest_metrics", cycle_id=cycle_id
                    )
        if (
            rest_poll_completed
            and self.external_trends is not None
            and not self.entries_paused
        ):
            try:
                await self.external_trends.poll()
            except Exception as exc:
                self._record_external_source_degradation(
                    exc, source="external_trends"
                )
        # News has an independent cadence and may include slow RSS/LLM work.
        # It must never serialize market admission or execution dispatch.
        news_task = asyncio.create_task(self._process_news(cycle_id))
        await self._process_market_strategies(cycle_id)
        try:
            await news_task
        except Exception as exc:
            self._record_failure(exc, stage="news_pipeline", cycle_id=cycle_id)
        self._sync_reservations()
        if (
            self.dependency_health.state == DependencyHealthState.HEALTHY
            or self.dependency_health.should_attempt()
        ):
            try:
                self._enforce_terminalization_invariants()
                self._monitor_positions(cycle_id)
            except Exception as exc:
                if not self._handle_dependency_failure(
                    exc, stage="position_monitoring", cycle_id=cycle_id
                ):
                    self._record_failure(
                        exc, stage="position_monitoring", cycle_id=cycle_id
                    )
        self.cycles += 1
        self.last_cycle_at = datetime.now(timezone.utc)
        self.last_error = (
            "cycle contained isolated failures"
            if sum(self.failure_occurrences.values()) > cycle_failures_before else None
        )
        await asyncio.to_thread(self._persist_runtime_metrics)
        self._update_drain_state()
        await asyncio.to_thread(self._refresh_status_snapshot)

    def refresh_universe_if_due(self) -> None:
        now = datetime.now(timezone.utc)
        if not self.dependency_health.should_attempt(now):
            return
        if self.universe.last_refresh_at is None or (
            now - self.universe.last_refresh_at
        ).total_seconds() >= self.settings.v2_universe_refresh_seconds:
            try:
                self.universe.refresh(now=now)
            except Exception as exc:
                if not self._handle_dependency_failure(
                    exc,
                    stage="universe_refresh",
                    cycle_id=f"{self.run_id}:universe-refresh",
                ):
                    raise

    async def _process_news(self, cycle_id: str) -> None:
        if self.entries_paused:
            return
        executable: list[V2SignalCandidate] = []
        self._increment(
            self.strategy_evaluation_counts, StrategyName.NEWS_MOMENTUM_V2.value
        )
        mapper = getattr(self.news_aggregator, "mapper", None)
        if mapper is not None and hasattr(mapper, "set_active_symbols"):
            mapper.set_active_symbols(self.universe.accepted_symbols)
        rows = await self.news_aggregator.poll()
        for audit in self.news_aggregator.last_poll_audit:
            self._persist_news_audit("V2_NEWS_ITEM_AUDIT", audit)
        self.news_metrics.update({
            "items_received": self.news_aggregator.items_received,
            "raw_news_feed_items_received": self.news_aggregator.items_received,
            "unique_news_items_discovered": getattr(
                self.news_aggregator, "unique_items_discovered",
                self.news_metrics.get("unique_news_items_discovered", 0),
            ),
            "items_deduplicated": self.news_aggregator.duplicate_count,
            "items_rejected_by_deterministic_filter": self.news_aggregator.items_rejected,
            "mapped_assets": sum(
                len(item.get("mapped_symbols") or [])
                for item in self.news_aggregator.last_poll_audit
                if item.get("deduplication_status") == "unique"
            ) + self.news_metrics.get("mapped_assets", 0),
        })
        strategy = next(
            row for row in self.strategies
            if row.name == StrategyName.NEWS_MOMENTUM_V2
        )
        for item, symbols, _fingerprint in rows:
            # Large feed batches must leave scheduling points for status and
            # shutdown requests while synchronous persistence is in progress.
            await asyncio.sleep(0)
            decision: dict[str, Any] = {
                "news_id": str(item.id), "llm_used": False, "model": None,
                "sentiment": None, "importance": item.importance,
                "urgency": None, "confidence": None,
                "mapped_symbols": [symbol.value for symbol in symbols],
                "market_confirmation_result": {}, "candidate_ids": [],
                "final_decision": "NO_CANDIDATE", "rejection_reason": None,
                "aggregator_filter_decision": "accepted",
                "classifier_prefilter_decision": None,
                "llm_decision_reason": None,
                "funnel_stage": "deterministic_accepted",
            }
            try:
                before_calls = self.v1_news_service.real_llm_calls_count
                classifier_before = self.v1_news_service.classifier_metrics_payload()
                accepted, filter_reason, classification = await asyncio.to_thread(
                    self.v1_news_service.ingest, item
                )
                classifier_after = self.v1_news_service.classifier_metrics_payload()
                decision["deterministic_filter_decision"] = filter_reason
                decision["classifier_prefilter_decision"] = filter_reason
                decision["llm_used"] = self.v1_news_service.real_llm_calls_count > before_calls
            except Exception as exc:
                self._record_failure(
                    exc, stage="news_classification", cycle_id=cycle_id,
                    source=item.source, input_field="news_item",
                )
                decision["rejection_reason"] = "classification_failed"
                self._persist_news_audit("V2_NEWS_DECISION_AUDIT", decision)
                continue
            if not accepted or classification is None:
                if not accepted:
                    self.news_metrics["items_rejected_by_deterministic_filter"] += 1
                    self.news_aggregator.items_rejected = self.news_metrics[
                        "items_rejected_by_deterministic_filter"
                    ]
                    source_metric = self.news_aggregator.source_metrics.get(item.source)
                    if source_metric is not None:
                        source_metric["deterministic_filter_accepts"] = max(
                            0, source_metric.get("deterministic_filter_accepts", 0) - 1
                        )
                        source_metric["deterministic_filter_rejections"] = (
                            source_metric.get("deterministic_filter_rejections", 0) + 1
                        )
                decision["rejection_reason"] = filter_reason
                decision["llm_decision_reason"] = _news_skip_reason(
                    accepted=accepted,
                    filter_reason=filter_reason,
                    classification=classification,
                    before=classifier_before,
                    after=classifier_after,
                )
                decision["funnel_stage"] = "deterministic_rejected" if not accepted else "llm_skipped"
                self._record_news_funnel_reason(
                    item.source, decision["llm_decision_reason"]
                )
                self._persist_news_audit("V2_NEWS_DECISION_AUDIT", decision)
                continue
            decision.update({
                "model": classification.model_name,
                "fallback_used": classification.fallback_used,
                "fallback_reason": classification.fallback_reason,
                "request_attempt_number": classification.request_attempt_number,
                "failure_category": classification.failure_category,
                "classification_status": classification.classification_status.value,
                "sentiment": classification.sentiment.value,
                "urgency": classification.urgency,
                "confidence": classification.confidence,
            })
            self._record_news_model_usage(classification)
            decision["llm_decision_reason"] = _news_classification_reason(
                classification, decision["llm_used"], classifier_before, classifier_after
            )
            decision["funnel_stage"] = "classified"
            self.news_metrics["llm_classifications"] += 1
            if decision["llm_used"]:
                self.news_metrics["items_sent_to_llm"] += 1
            source_metric = self.news_aggregator.source_metrics.get(item.source)
            if source_metric is not None:
                source_metric["classified_items"] = source_metric.get("classified_items", 0) + 1
                source_metric["items_sent_to_llm"] = source_metric.get("items_sent_to_llm", 0) + int(decision["llm_used"])
                if decision["llm_decision_reason"] == "classifier_cache_hit":
                    source_metric["llm_cache_hits"] = source_metric.get("llm_cache_hits", 0) + 1
                elif decision["llm_decision_reason"] == "classifier_budget_rejected":
                    source_metric["llm_budget_rejections"] = source_metric.get("llm_budget_rejections", 0) + 1
                elif decision["llm_decision_reason"] == "classifier_circuit_breaker_open":
                    source_metric["llm_circuit_breaker_rejections"] = source_metric.get("llm_circuit_breaker_rejections", 0) + 1
                elif str(decision["llm_decision_reason"]).startswith("classifier_failed"):
                    source_metric["classifier_failures"] = source_metric.get("classifier_failures", 0) + 1
            if not classification.trade_eligible:
                decision["rejection_reason"] = "classification_not_trade_eligible"
                decision["funnel_stage"] = "classified_not_trade_eligible"
                self._persist_news_audit("V2_NEWS_DECISION_AUDIT", decision)
                continue
            self.news_metrics["trade_eligible_items"] += 1
            decision["funnel_stage"] = "trade_eligible"
            if source_metric is not None:
                source_metric["trade_eligible_items"] = source_metric.get("trade_eligible_items", 0) + 1
            for symbol in symbols:
                await asyncio.sleep(0)
                if symbol not in self.universe.accepted_symbols:
                    decision["market_confirmation_result"][symbol.value] = "symbol_not_accepted"
                    continue
                if not strategy.enabled or not strategy.applies_to(symbol):
                    decision["market_confirmation_result"][symbol.value] = "strategy_not_applicable"
                    self._increment(self.strategy_not_applicable_counts, strategy.name.value)
                    continue
                try:
                    feature = self._feature(symbol)
                    if feature is None:
                        decision["market_confirmation_result"][symbol.value] = "feature_unavailable"
                        continue
                    self._increment(self.strategy_evaluation_counts, strategy.name.value)
                    context = NewsStrategyContext(
                        sentiment=classification.sentiment.value,
                        confidence=Decimal(str(classification.confidence)),
                        importance=Decimal(str(item.importance)),
                        news_ids=(str(item.id),), market_wide=len(symbols) > 1,
                    )
                    candidate = self._admit(strategy.evaluate(feature, news=context))
                    decision["candidate_ids"].append(str(candidate.id))
                    decision["market_confirmation_result"][symbol.value] = candidate.state
                    self.news_metrics["news_momentum_candidates_generated"] += 1
                    if source_metric is not None:
                        source_metric["candidates_generated"] = source_metric.get("candidates_generated", 0) + 1
                    if candidate.admitted:
                        executable.append(candidate)
                        self.news_metrics["news_momentum_candidates_admitted"] += 1
                        if source_metric is not None:
                            source_metric["candidates_admitted"] = source_metric.get("candidates_admitted", 0) + 1
                except Exception as exc:
                    self._record_failure(
                        exc, stage="news_strategy_evaluation", cycle_id=cycle_id,
                        symbol=symbol, strategy=strategy.name.value,
                        source=item.source,
                    )
                    decision["market_confirmation_result"][symbol.value] = "evaluation_failed"
            decision["final_decision"] = (
                "CANDIDATE_CREATED" if decision["candidate_ids"] else "NO_CANDIDATE"
            )
            if decision["candidate_ids"]:
                decision["funnel_stage"] = (
                    "admitted" if any(
                        value == "READY"
                        for value in decision["market_confirmation_result"].values()
                    ) else "candidate"
                )
            if not decision["candidate_ids"] and not decision["rejection_reason"]:
                decision["rejection_reason"] = "no_accepted_symbol_with_fresh_market_confirmation"
            self._persist_news_audit("V2_NEWS_DECISION_AUDIT", decision)
        await self._execute_concurrently(
            self._select_ranked_candidates(executable), cycle_id
        )

    async def _process_market_strategies(self, cycle_id: str) -> None:
        if self.entries_paused:
            return
        executable: list[V2SignalCandidate] = []
        dispatches: list[tuple[V2SignalCandidate, asyncio.Future[Any]]] = []
        dispatched_count = 0
        for symbol in self.universe.accepted_symbols:
            # Candidate persistence is synchronous and can involve multiple DB
            # round trips. Cooperatively yield between full-universe items.
            await asyncio.sleep(0)
            metric = self._symbol_metric(symbol)
            metric["cycles_attempted"] += 1
            symbol_failed = False
            try:
                feature = self._feature(symbol)
            except Exception as exc:
                metric["cycles_failed"] += 1
                metric["latest_failure_category"] = type(exc).__name__
                self._record_failure(
                    exc, stage="feature_generation", cycle_id=cycle_id, symbol=symbol,
                    input_field=_input_field_for_exception(exc),
                )
                continue
            if feature is None:
                metric["cycles_failed"] += 1
                metric["latest_failure_category"] = "FEATURE_UNAVAILABLE"
                continue
            metric["latest_feature_timestamp"] = feature.timestamp.isoformat()
            self._update_liquidation_symbol_status(feature)
            for strategy in self.strategies:
                await asyncio.sleep(0)
                if not strategy.enabled or strategy.name == StrategyName.NEWS_MOMENTUM_V2:
                    continue
                if not strategy.applies_to(symbol):
                    self._increment(self.strategy_not_applicable_counts, strategy.name.value)
                    continue
                if strategy.name == StrategyName.LIQUIDATION_MOMENTUM:
                    liquidation_reason = self._liquidation_not_applicable_reason(feature)
                    if liquidation_reason is not None:
                        self._increment(
                            self.strategy_not_applicable_counts, strategy.name.value
                        )
                        self.liquidation_metrics["not_applicable_count"] += 1
                        self._increment(
                            self.liquidation_metrics["reasons"], liquidation_reason
                        )
                        continue
                    self.liquidation_metrics["applicable_count"] += 1
                    self.liquidation_metrics["last_valid_liquidation_timestamp"] = (
                        feature.liquidation_last_valid_at.isoformat()
                        if feature.liquidation_last_valid_at else None
                    )
                    self.liquidation_metrics["current_liquidation_data_age_seconds"] = (
                        feature.liquidation_data_age_seconds
                    )
                metric["strategies_evaluated"] += 1
                self._increment(self.strategy_evaluation_counts, strategy.name.value)
                try:
                    if strategy.name == StrategyName.MEME_TREND:
                        trend_available = bool(
                            self.external_trends
                            and self.external_trends.health.value == "OK"
                            and self.external_trends.last_updated_at is not None
                            and (
                                datetime.now(timezone.utc)
                                - self.external_trends.last_updated_at
                            ).total_seconds()
                            <= self.settings.v2_rest_data_stale_seconds
                        )
                        candidate = strategy.evaluate(feature, meme=MemeTrendContext(
                            Decimal(str(self.external_trends.score(symbol)))
                            if self.external_trends else Decimal("0"),
                            available=trend_available,
                            observed_at=(
                                self.external_trends.last_updated_at
                                if self.external_trends else None
                            ),
                        ))
                    else:
                        candidate = strategy.evaluate(feature)
                    admitted = self._admit(candidate)
                    metric["candidates_generated"] += 1
                    if admitted.admitted:
                        metric["candidates_admitted"] += 1
                        executable.append(admitted)
                    else:
                        metric["candidates_rejected"] += 1
                except Exception as exc:
                    symbol_failed = True
                    metric["latest_failure_category"] = type(exc).__name__
                    self._record_failure(
                        exc, stage="strategy_evaluation", cycle_id=cycle_id,
                        symbol=symbol, strategy=strategy.name.value,
                        input_field=_input_field_for_exception(exc),
                    )
            if (
                executable
                and self.settings.v2_auto_demo_execution
                and dispatched_count < self.settings.v2_max_entries_per_cycle
            ):
                remaining = self.settings.v2_max_entries_per_cycle - dispatched_count
                for selected in self._select_ranked_candidates(
                    executable, limit=remaining
                ):
                    dispatched = self._dispatch_now(selected)
                    if dispatched is not None:
                        dispatches.append((selected, dispatched))
                        dispatched_count += 1
                executable = []
            if symbol_failed:
                metric["cycles_failed"] += 1
            else:
                metric["cycles_succeeded"] += 1
                metric["latest_success_timestamp"] = datetime.now(timezone.utc).isoformat()
                metric["latest_failure_category"] = None
        if self.settings.v2_auto_demo_execution:
            await self._collect_dispatches(dispatches, cycle_id)
        else:
            self._select_ranked_candidates(executable)

    def _feature(self, symbol: Symbol) -> Any | None:
        btc = None
        if symbol != Symbol.BTCUSDT:
            btc = self.features.snapshot(Symbol.BTCUSDT)
        feature = self.features.snapshot(symbol, btc_snapshot=btc)
        if feature:
            self.repository.save_v2_market_feature(feature)
        return feature

    def _admit(self, candidate: V2SignalCandidate) -> V2SignalCandidate:
        candidate.run_id = self.run_id
        estimate = self.edge_calibrator.estimate(candidate)
        candidate.meta_label_status = estimate.status
        candidate.meta_label_probability = estimate.win_probability_lower_bound
        if estimate.ready and estimate.expected_net_edge_bps is not None:
            candidate.edge_calibrated = True
            candidate.estimated_edge_bps = min(
                self.settings.v2_max_empirical_edge_bps,
                max(
                    Decimal("0"),
                    estimate.expected_net_edge_bps
                    + candidate.expected_fees_bps
                    + candidate.expected_slippage_bps
                    + candidate.expected_funding_bps,
                ),
            )
        self._record_signal_metric("raw_candidates", candidate)
        # Admission asks whether at least the minimum safe position can fit.
        # Exact confidence/edge/risk sizing is performed after account fees are
        # refreshed immediately before the durable reservation.
        notional = self.settings.v2_min_position_notional_usdt
        risk_usdt = notional * candidate.stop_loss_pct / Decimal("100")
        try:
            portfolio_reasons = self.portfolio.block_reasons(
                candidate.symbol,
                notional,
                risk_usdt=risk_usdt,
                side=candidate.side,
                btc_beta=candidate.feature_snapshot.btc_beta,
            )
        except TypeError:
            # Lightweight read-only test/report portfolios may implement the
            # original two-argument protocol.
            portfolio_reasons = self.portfolio.block_reasons(
                candidate.symbol, notional
            )
        status = self.universe.get(candidate.symbol)
        exposure = sum(
            row.notional_usdt for row in self.portfolio.reservations
            if row.state in self.portfolio.ACTIVE
        ) / self.settings.max_total_notional_usdt
        group = correlation_group(candidate.symbol)
        group_count = sum(
            row.correlation_group == group for row in self.portfolio.reservations
            if row.state in self.portfolio.ACTIVE
        )
        correlation_penalty = (
            Decimal(group_count)
            / Decimal(self.settings.max_positions_per_correlation_group)
            * Decimal("0.15")
        )
        candidate = self.scoring.admit(
            candidate, symbol_valid=bool(status and status.accepted),
            portfolio_reasons=portfolio_reasons,
            context=AdmissionContext(
                correlation_penalty=correlation_penalty,
                portfolio_exposure_penalty=exposure * Decimal("0.15"),
            ),
        )
        if candidate.distance_to_threshold >= 0:
            self._record_signal_metric("threshold_passes", candidate)
        if portfolio_reasons:
            self._record_signal_metric("portfolio_rejections", candidate)
        signature = _candidate_signature(candidate)
        prior = self._candidate_signatures.get(signature)
        deduplication_window = max(
            timedelta(seconds=5), candidate.expires_at - candidate.created_at
        )
        if prior is not None and candidate.created_at - prior < deduplication_window:
            candidate.admitted = False
            candidate.state = "DEDUPLICATED"
            candidate.rejection_reason = "duplicate unchanged candidate"
            self._record_signal_metric("deduplicated_candidates", candidate)
            return candidate
        self._candidate_signatures[signature] = candidate.created_at
        candidate.candidate_persisted_at = datetime.now(timezone.utc)
        self.repository.save_v2_signal_candidate(candidate)
        self.candidates.append(candidate)
        if candidate.admitted:
            self._record_signal_metric("pre_execution_admissions", candidate)
        if candidate.feature_snapshot.stale_evidence and not candidate.admitted:
            self.stale_metrics["stale_feature_rejections"] += 1
            self._increment(
                self.stale_metrics["stale_rejections_by_symbol"], candidate.symbol.value
            )
            self._increment(
                self.stale_metrics["stale_rejections_by_strategy"],
                candidate.strategy_name.value,
            )
            for evidence in candidate.feature_snapshot.stale_evidence:
                self._increment(
                    self.stale_metrics["stale_rejections_by_source"],
                    str(evidence.get("source") or "unknown"),
                )
        self.logger.info(
            "V2 signal admitted" if candidate.admitted else "V2 signal rejected",
            extra={
                "run_id": self.run_id, "candidate_id": str(candidate.id),
                "strategy": candidate.strategy_name.value,
                "symbol": candidate.symbol.value,
                "event_type": "SIGNAL_ADMITTED" if candidate.admitted else "SIGNAL_REJECTED",
                "execution_environment": "BYBIT_DEMO",
            },
        )
        return candidate

    def _restore_calibration_history(self) -> None:
        loader = getattr(self.repository, "load_demo_executions", None)
        candidate_loader = getattr(
            self.repository, "load_v2_calibration_candidates", None
        )
        if not callable(candidate_loader):
            legacy_loader = getattr(
                self.repository, "load_v2_signal_candidates", None
            )
            candidate_loader = (
                (lambda: legacy_loader(None))
                if callable(legacy_loader) else None
            )
        if not callable(loader) or not callable(candidate_loader):
            return
        try:
            candidates = {
                str(item.id): item for item in candidate_loader()
            }
            observations: list[CalibrationObservation] = []
            for record in loader():
                candidate = candidates.get(str(record.candidate_id))
                opened_at = (
                    record.first_fill_at
                    or record.exchange_fill_at
                    or record.position_confirmed_at
                    or record.created_at
                )
                if (
                    candidate is None
                    or record.closed_at is None
                    or record.realized_exchange_pnl is None
                    or record.average_fill_price is None
                    or record.accepted_quantity <= 0
                ):
                    continue
                notional = record.average_fill_price * record.accepted_quantity
                if notional <= 0:
                    continue
                observations.append(CalibrationObservation(
                    strategy=candidate.strategy_name.value,
                    symbol=candidate.symbol.value,
                    regime=candidate.market_regime,
                    net_return_bps=(
                        record.realized_exchange_pnl / notional * Decimal("10000")
                    ),
                    opened_at=opened_at,
                    closed_at=record.closed_at,
                ))
            self.edge_calibrator.fit(observations)
        except Exception:
            # Calibration is a shadow enhancement. Any restore uncertainty
            # leaves the deterministic gates intact and never enables a trade.
            self.edge_calibrator.fit([])

    def _select_ranked_candidates(
        self, candidates: list[V2SignalCandidate], *, limit: int | None = None,
    ) -> list[V2SignalCandidate]:
        ranked = self.scoring.rank(candidates)
        selection_limit = (
            self.settings.v2_max_entries_per_cycle if limit is None else limit
        )
        selected = ranked[:selection_limit]
        for candidate in ranked[selection_limit:]:
            candidate.admitted = False
            candidate.state = "RANKING_REJECTED"
            candidate.rejection_reason = (
                "not selected by current-cycle net-edge ranking"
            )
        for candidate in ranked:
            self.repository.save_v2_signal_candidate(candidate)
        return selected

    async def _execute_concurrently(
        self, candidates: list[V2SignalCandidate], cycle_id: str
    ) -> None:
        if not self.settings.v2_auto_demo_execution or self.entries_paused:
            return
        dispatches = [
            (candidate, future)
            for candidate in candidates
            if (future := self._dispatch_now(candidate)) is not None
        ]
        await self._collect_dispatches(dispatches, cycle_id)

    def _dispatch_now(
        self, candidate: V2SignalCandidate,
    ) -> asyncio.Future[Any] | None:
        if not self.settings.v2_auto_demo_execution or self.entries_paused:
            return None
        candidate.execution_queue_entered_at = datetime.now(timezone.utc)
        self.repository.save_v2_signal_candidate(candidate)
        executor = self._execution_pools.setdefault(
            candidate.symbol,
            ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"v2-exec-{candidate.symbol.value.lower()}",
            ),
        )
        return asyncio.get_running_loop().run_in_executor(
            executor, self.execution.execute, candidate
        )

    async def _collect_dispatches(
        self,
        dispatches: list[tuple[V2SignalCandidate, asyncio.Future[Any]]],
        cycle_id: str,
    ) -> None:
        if not dispatches:
            return
        results = await asyncio.gather(
            *(future for _, future in dispatches), return_exceptions=True
        )
        for (candidate, _), result in zip(dispatches, results):
            if isinstance(result, Exception):
                self._record_failure(
                    result, stage="demo_execution", cycle_id=cycle_id,
                    symbol=candidate.symbol, strategy=candidate.strategy_name.value,
                )
            elif isinstance(result, dict) and result.get("handled_pre_submit_rejection"):
                self._record_pre_submit_rejection(candidate, result)
                if result.get("handled_external_dependency_rejection"):
                    message = str(
                        result.get("rejection_message")
                        or "Demo REST entry preflight is unavailable"
                    )
                    category = str(
                        result.get("dependency_error_category") or "TRANSPORT"
                    )
                    if category == "DNS_RESOLUTION":
                        message = f"getaddrinfo failed: {message}"
                    elif category == "TIMEOUT":
                        message = f"timed out: {message}"
                    self._handle_dependency_failure(
                        ConnectionError(message),
                        stage="demo_execution_pre_mutation",
                        cycle_id=cycle_id,
                    )
            elif isinstance(result, dict) and result.get("handled_policy_rejection"):
                self._record_execution_policy_rejection(candidate, result)
            elif isinstance(result, dict) and result.get("handled_persistence_rejection"):
                self._record_execution_persistence_rejection(candidate, result)
            elif isinstance(result, dict) and result.get("execution_id"):
                self._record_signal_metric("admitted_signals", candidate)

    def _record_pre_submit_rejection(
        self,
        candidate: V2SignalCandidate,
        result: dict[str, Any],
    ) -> None:
        rejected_at = _aware_utc_datetime(result.get("rejected_at"))
        code = str(result.get("rejection_code") or "FINAL_MARKET_REJECTED")
        candidate_key = str(candidate.id)
        if candidate_key in self._handled_pre_submit_rejections:
            return
        incident = V2Incident(
            id=uuid5(
                NAMESPACE_URL,
                f"bybot-v2-pre-submit-rejection:{self.run_id}:{candidate.id}",
            ),
            run_id=self.run_id,
            event_type="PRE_SUBMIT_ENTRY_REJECTED",
            symbol=candidate.symbol,
            candidate_id=candidate.id,
            error_category=code,
            payload={
                "candidate_id": str(candidate.id),
                "signal_id": result.get("signal_id"),
                "reservation_id": result.get("reservation_id"),
                "execution_id": None,
                "symbol": candidate.symbol.value,
                "strategy": candidate.strategy_name.value,
                "rejection_code": code,
                "rejection_message": result.get("rejection_message"),
                "processing_stage": "final_pre_submit_market_gate",
                "pre_submit_audit": result.get("pre_submit_audit") or {},
                "reservation_release_result": result.get(
                    "reservation_release_result"
                ),
                "exchange_mutation_performed": False,
                "exchange_order_submission_invoked": False,
                "rejected_at": rejected_at.isoformat(),
            },
            occurred_at=rejected_at,
        )
        if not self.repository.save_v2_incident(incident):
            raise RuntimeError("pre-submit rejection incident persistence failed")
        self._handled_pre_submit_rejections.add(candidate_key)
        self._record_signal_metric("pre_submit_rejections", candidate)
        if code in {
            "FINAL_EXECUTABLE_DEPTH_INSUFFICIENT",
            "FINAL_EXECUTABLE_DEPTH_MISSING",
        }:
            self._record_signal_metric("final_depth_rejections", candidate)
        by_code = self.signal_metrics.setdefault("pre_submit_rejections_by_code", {})
        by_code[code] = int(by_code.get(code) or 0) + 1
        self.logger.info(
            "V2 final pre-submit rejected candidate: %s",
            result.get("rejection_message"),
            extra={
                "event_timestamp": rejected_at,
                "run_id": self.run_id,
                "candidate_id": str(candidate.id),
                "strategy": candidate.strategy_name.value,
                "symbol": candidate.symbol.value,
                "event_type": "PRE_SUBMIT_ENTRY_REJECTED",
                "execution_environment": "BYBIT_DEMO",
                "error_category": code,
                "processing_stage": "final_pre_submit_market_gate",
            },
        )

    def _record_execution_persistence_rejection(
        self,
        candidate: V2SignalCandidate,
        result: dict[str, Any],
    ) -> None:
        occurred_at = datetime.now(timezone.utc)
        error_code = str(result.get("rejection_code") or "DB_COMPATIBILITY_BUNDLE_FAILED")
        self._record_signal_metric("persistence_rejections", candidate)
        reason = "execution compatibility persistence failed"
        if reason not in self.run_invalid_reasons:
            self.run_invalid_reasons.append(reason)
        self.run_valid = False
        incident = V2Incident(
            id=uuid5(
                NAMESPACE_URL,
                f"bybot-v2-persistence-rejection:{self.run_id}:{candidate.id}",
            ),
            run_id=self.run_id,
            event_type="EXECUTION_PERSISTENCE_REJECTED",
            symbol=candidate.symbol,
            candidate_id=candidate.id,
            error_category=error_code,
            payload={
                "candidate_id": str(candidate.id),
                "symbol": candidate.symbol.value,
                "strategy": candidate.strategy_name.value,
                "processing_stage": "compatibility_persistence",
                "error_code": error_code,
                "exchange_mutation_performed": False,
            },
            occurred_at=occurred_at,
        )
        self.repository.save_v2_incident(incident)
        self.logger.error(
            "V2 execution compatibility persistence rejected candidate: %s",
            error_code,
            extra={
                "event_timestamp": occurred_at,
                "run_id": self.run_id,
                "candidate_id": str(candidate.id),
                "strategy": candidate.strategy_name.value,
                "symbol": candidate.symbol.value,
                "event_type": "EXECUTION_PERSISTENCE_REJECTED",
                "execution_environment": "BYBIT_DEMO",
                "error_category": error_code,
                "processing_stage": "compatibility_persistence",
            },
        )

    def _record_execution_policy_rejection(
        self,
        candidate: V2SignalCandidate,
        result: dict[str, Any],
    ) -> None:
        rejected_at = _aware_utc_datetime(result.get("rejected_at"))
        self._record_signal_metric("execution_policy_rejections", candidate)
        if result.get("rejection_code") in {
            "SYMBOL_COOLDOWN_ACTIVE", "GLOBAL_ENTRY_COOLDOWN_ACTIVE",
        }:
            self._record_signal_metric("cooldown_rejections", candidate)
        incident = V2Incident(
            id=uuid5(
                NAMESPACE_URL,
                f"bybot-v2-policy-rejection:{self.run_id}:{candidate.id}",
            ),
            run_id=self.run_id,
            event_type="EXECUTION_REJECTED",
            symbol=candidate.symbol,
            candidate_id=candidate.id,
            payload={
                "candidate_id": str(candidate.id),
                "reservation_id": result.get("reservation_id"),
                "execution_id": None,
                "symbol": candidate.symbol.value,
                "strategy": candidate.strategy_name.value,
                "rejection_code": result.get("rejection_code"),
                "rejection_message": result.get("rejection_message"),
                "risk_control": result.get("risk_control"),
                "processing_stage": "demo_execution",
                "rejected_at": rejected_at.isoformat(),
                "exchange_mutation_performed": False,
            },
            occurred_at=rejected_at,
        )
        self.repository.save_v2_incident(incident)
        self.logger.info(
            "V2 execution policy rejected candidate: %s",
            result.get("rejection_message"),
            extra={
                "event_timestamp": rejected_at,
                "run_id": self.run_id,
                "candidate_id": str(candidate.id),
                "strategy": candidate.strategy_name.value,
                "symbol": candidate.symbol.value,
                "event_type": "EXECUTION_REJECTED",
                "execution_environment": "BYBIT_DEMO",
                "error_category": result.get("rejection_code"),
                "processing_stage": "demo_execution",
            },
        )

    def _liquidation_not_applicable_reason(
        self, feature: Any,
    ) -> str | None:
        if not feature.liquidation_data_valid:
            return "liquidation_data_invalid"
        if not feature.liquidation_feed_initialized:
            return "liquidation_feed_never_initialized"
        if not feature.liquidation_feed_available:
            return "liquidation_feed_unavailable"
        # Liquidation events are sparse by nature. Once the transport and
        # subscription are healthy, no recent event means zero intensity, not
        # stale transport. Connection/heartbeat health owns staleness.
        return None

    def _update_liquidation_symbol_status(self, feature: Any) -> None:
        reason = self._liquidation_not_applicable_reason(feature)
        self.liquidation_metrics["by_symbol"][feature.symbol.value] = {
            "symbol": feature.symbol.value,
            "last_valid_timestamp": (
                feature.liquidation_last_valid_at.isoformat()
                if feature.liquidation_last_valid_at else None
            ),
            "observed_age_seconds": feature.liquidation_data_age_seconds,
            "state": "ELIGIBLE" if reason is None else "INELIGIBLE",
            "not_applicable_reason": reason,
            "connection_state": feature.liquidation_connection_state,
            "subscription_state": feature.liquidation_subscription_state,
            "rolling_event_count": feature.liquidation_event_count_5m,
            "rolling_liquidation_notional": str(
                feature.liquidation_notional_5m
            ),
        }

    def _liquidation_metrics_at(self, generated_at: datetime) -> dict[str, Any]:
        metrics = dict(self.liquidation_metrics)
        by_symbol: dict[str, dict[str, Any]] = {}
        timestamps: list[datetime] = []
        for symbol, value in (self.liquidation_metrics.get("by_symbol") or {}).items():
            row = dict(value)
            stamp_text = row.get("last_valid_timestamp")
            stamp = datetime.fromisoformat(stamp_text) if stamp_text else None
            age = max(0.0, (generated_at - stamp).total_seconds()) if stamp else None
            row["current_age_seconds"] = age
            row.pop("observed_age_seconds", None)
            by_symbol[symbol] = row
            if stamp:
                timestamps.append(stamp)
        metrics["liquidation_eligibility_by_symbol"] = by_symbol
        metrics["by_symbol"] = by_symbol
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
        metrics["age_calculated_at"] = generated_at.isoformat()
        # Remove the old cycle-local scalar that caused cross-symbol ambiguity.
        metrics.pop("current_liquidation_data_age_seconds", None)
        metrics.pop("last_valid_liquidation_timestamp", None)
        return metrics

    def _symbol_metric(self, symbol: Symbol) -> dict[str, Any]:
        return self.symbol_cycle_metrics.setdefault(symbol.value, {
            "cycles_attempted": 0, "cycles_succeeded": 0, "cycles_failed": 0,
            "latest_feature_timestamp": None, "latest_success_timestamp": None,
            "latest_failure_category": None, "strategies_evaluated": 0,
            "candidates_generated": 0, "candidates_rejected": 0,
            "candidates_admitted": 0,
        })

    @staticmethod
    def _increment(values: dict[str, int], key: str) -> None:
        values[key] = values.get(key, 0) + 1

    def _record_failure(
        self,
        exc: Exception,
        *,
        stage: str,
        cycle_id: str,
        symbol: Symbol | None = None,
        strategy: str | None = None,
        source: str | None = None,
        input_field: str | None = None,
    ) -> None:
        outcome = typed_outcome(exc)
        if outcome is not None and outcome.classification in NON_FAILURE_OUTCOMES:
            self._record_non_failure_outcome(
                exc,
                stage=stage,
                cycle_id=cycle_id,
                symbol=symbol,
                strategy=strategy,
                source=source,
                input_field=input_field,
            )
            return
        message = _sanitize_runtime_error(str(exc))
        fingerprint = _failure_fingerprint(
            exc, stage=stage, symbol=symbol, strategy=strategy
        )
        count = self.failure_occurrences.get(fingerprint, 0) + 1
        self.failure_occurrences[fingerprint] = count
        critical_key = (
            outcome.classification.value
            if outcome is not None
            else "UNEXPECTED_CYCLE_FAILURE"
        )
        self._increment(self.critical_classification_counts, critical_key)
        now = datetime.now(timezone.utc)
        payload = {
            "exception_class": type(exc).__name__, "message": message,
            "processing_stage": stage,
            "symbol": symbol.value if symbol else None,
            "strategy": strategy, "source": source, "cycle_id": cycle_id,
            "traceback_fingerprint": fingerprint,
            "relevant_input_field": input_field,
            "transient": _is_transient_failure(exc),
            "occurrence_count": count, "last_seen_at": now.isoformat(),
            "outcome_classification": (
                outcome.classification.value
                if outcome is not None
                else "UNEXPECTED_CYCLE_FAILURE"
            ),
            "outcome_code": (
                outcome.code if outcome is not None else "UNHANDLED_EXCEPTION"
            ),
            "exchange_mutation_attempted": (
                outcome.exchange_mutation_attempted
                if outcome is not None else None
            ),
            "outcome_evidence": outcome.evidence if outcome is not None else {},
        }
        incident = V2Incident(
            id=uuid5(NAMESPACE_URL, f"bybot-v2-failure:{self.run_id}:{fingerprint}"),
            run_id=self.run_id, event_type="V2_CYCLE_FAILURE", symbol=symbol,
            error_category=type(exc).__name__, payload=payload, occurred_at=now,
        )
        self.repository.save_v2_incident(incident)
        self.failure_circuit_breaker_active = True
        self.stop_new_entries = True
        self.run_valid = False
        reason = (
            f"{payload['outcome_classification']}:"
            f"{payload['outcome_code']}:{stage}/{fingerprint}"
        )
        if reason not in self.run_invalid_reasons:
            self.run_invalid_reasons.append(reason)
        self.logger.exception(
            "V2 isolated failure: %s", message,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "event_timestamp": now,
                "run_id": self.run_id, "strategy": strategy,
                "symbol": symbol.value if symbol else None,
                "event_type": "V2_CYCLE_FAILURE",
                "execution_environment": "BYBIT_DEMO",
                "error_category": type(exc).__name__,
                "processing_stage": stage, "source": source,
                "traceback_fingerprint": fingerprint, "cycle_id": cycle_id,
            },
        )

    def _record_non_failure_outcome(
        self,
        exc: RuntimeOutcomeError,
        *,
        stage: str,
        cycle_id: str,
        symbol: Symbol | None = None,
        strategy: str | None = None,
        source: str | None = None,
        input_field: str | None = None,
        execution_id: Any | None = None,
    ) -> None:
        outcome = exc.details
        now = datetime.now(timezone.utc)
        fingerprint = hashlib.sha256(
            (
                f"{self.run_id}:{outcome.classification.value}:{outcome.code}:"
                f"{stage}:{symbol.value if symbol else ''}:"
                f"{strategy or ''}:{execution_id or ''}"
            ).encode()
        ).hexdigest()[:24]
        count = self.outcome_occurrences.get(fingerprint, 0) + 1
        self.outcome_occurrences[fingerprint] = count
        self._increment(
            self.outcome_classification_counts,
            outcome.classification.value,
        )
        self._increment(self.outcome_code_counts, outcome.code)
        first_seen = self.outcome_first_seen.setdefault(
            fingerprint, now.isoformat()
        )
        event_type = {
            RuntimeOutcome.SAFE_DEGRADED: "V2_SAFE_DEGRADED",
            RuntimeOutcome.EXPECTED_REJECTION: "V2_EXPECTED_REJECTION",
            RuntimeOutcome.OBSERVABILITY_WARNING: "V2_OBSERVABILITY_WARNING",
            RuntimeOutcome.SUCCESS: "V2_SUCCESS",
        }[outcome.classification]
        payload = {
            "classification": outcome.classification.value,
            "code": outcome.code,
            "message": _sanitize_runtime_error(str(exc)),
            "processing_stage": stage,
            "cycle_id": cycle_id,
            "symbol": symbol.value if symbol else None,
            "strategy": strategy,
            "source": source,
            "relevant_input_field": input_field,
            "first_seen_at": first_seen,
            "last_seen_at": now.isoformat(),
            "occurrence_count": count,
            "exchange_mutation_attempted": outcome.exchange_mutation_attempted,
            "evidence": outcome.evidence,
        }
        saved = self.repository.save_v2_incident(V2Incident(
            id=uuid5(
                NAMESPACE_URL,
                f"bybot-v2-outcome:{self.run_id}:{fingerprint}",
            ),
            run_id=self.run_id,
            event_type=event_type,
            symbol=symbol,
            execution_id=execution_id,
            error_category=outcome.classification.value,
            payload=payload,
            occurred_at=now,
        ))
        if saved is False:
            self._record_failure(
                DataIntegrityCriticalOutcome(
                    "typed runtime outcome persistence failed",
                    code="RUNTIME_OUTCOME_PERSISTENCE_FAILED",
                ),
                stage="outcome_persistence",
                cycle_id=cycle_id,
                symbol=symbol,
                strategy=strategy,
            )
            return
        self.logger.warning(
            "V2 %s: %s",
            outcome.classification.value,
            payload["message"],
            extra={
                "event_timestamp": now,
                "run_id": self.run_id,
                "strategy": strategy,
                "symbol": symbol.value if symbol else None,
                "event_type": event_type,
                "execution_environment": "BYBIT_DEMO",
                "error_category": outcome.code,
                "processing_stage": stage,
                "cycle_id": cycle_id,
            },
        )

    def _handle_dependency_failure(
        self, exc: Exception, *, stage: str, cycle_id: str
    ) -> bool:
        active_count, protected = self._active_protection_state()
        host = (
            "api.bybit.com"
            if stage in {"rest_metrics", "universe_refresh"}
            else "api-demo.bybit.com"
        )
        decision = self.dependency_health.record_failure(
            exc,
            dependency="bybit_rest",
            host=host,
            active_position_count=active_count,
            protection_confirmed=protected,
        )
        if not decision.handled:
            return False
        self.last_error = "external dependency is temporarily degraded"
        if decision.hard_failure:
            blocker = (
                "Bybit REST outage exceeded the bounded safety window"
                if protected
                else "Bybit REST outage left protection unconfirmed"
            )
            self.stop_new_entries = True
            self.run_valid = False
            if blocker not in self.run_invalid_reasons:
                self.run_invalid_reasons.append(blocker)
            self._record_failure(
                ExternalDependencySafetyError(blocker),
                stage="external_dependency_hard_failure",
                cycle_id=cycle_id,
            )
        return True

    def _active_protection_state(self) -> tuple[int, bool]:
        terminal = {
            "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE",
            "DEMO_NOT_SUBMITTED", "DEMO_ORDER_CANCELLED",
            "DEMO_CLOSED_AFTER_INTERRUPTION", "DEMO_CLOSED_EXTERNALLY",
            "DEMO_FAILED_FLAT_VERIFIED", "DEMO_FAILED",
        }
        active = [
            item for item in self.repository.load_demo_executions()
            if item.state.value not in terminal and item.accepted_quantity > 0
        ]
        return len(active), all(item.protection_confirmed for item in active)

    def _record_external_source_degradation(
        self, exc: Exception, *, source: str
    ) -> None:
        now = datetime.now(timezone.utc)
        fingerprint = hashlib.sha256(
            f"{self.run_id}:{source}:{type(exc).__name__}".encode()
        ).hexdigest()[:24]
        self.repository.save_v2_incident(V2Incident(
            id=uuid5(
                NAMESPACE_URL,
                f"bybot-v2-external-source:{self.run_id}:{fingerprint}",
            ),
            run_id=self.run_id,
            event_type="EXTERNAL_SOURCE_DEGRADED",
            error_category=type(exc).__name__,
            payload={
                "source": source,
                "message": _sanitize_runtime_error(str(exc)),
                "entries_paused": False,
                "last_seen_at": now.isoformat(),
            },
            occurred_at=now,
        ))

    def _persist_news_audit(self, event_type: str, payload: dict[str, Any]) -> None:
        identity = str(payload.get("news_id") or "unknown")
        fingerprint = hashlib.sha256(
            f"{event_type}:{identity}:{payload.get('deduplication_status')}:{payload.get('final_decision')}".encode()
        ).hexdigest()
        self.repository.save_v2_incident(V2Incident(
            id=uuid5(NAMESPACE_URL, f"bybot-v2-news:{self.run_id}:{fingerprint}"),
            run_id=self.run_id, event_type=event_type, payload=payload,
        ))

    def _record_news_model_usage(self, classification: Any) -> None:
        if classification.provider_name == "deterministic-v2":
            return
        if classification.model_name not in {
            self.settings.news_primary_model, self.settings.news_fallback_model
        }:
            return
        self.last_news_model_usage = NewsModelUsage(
            news_id=classification.news_id,
            model=classification.model_name,
            fallback_used=classification.fallback_used,
            fallback_reason=classification.fallback_reason,
            classified_at=classification.classified_at,
        )
        with self._status_lock:
            self._status_snapshot["last_news_model_usage"] = (
                self.last_news_model_usage.model_dump(mode="json")
            )
            self._status_snapshot["last_news_model_used"] = (
                self.last_news_model_usage.model
            )
            self._status_snapshot["last_news_fallback_used"] = (
                self.last_news_model_usage.fallback_used
            )

    def _record_news_funnel_reason(self, source: str, reason: str) -> None:
        reasons = self.news_metrics.setdefault("news_funnel_reasons", {})
        reasons[reason] = int(reasons.get(reason) or 0) + 1
        source_metric = self.news_aggregator.source_metrics.get(source)
        if source_metric is None:
            return
        key = {
            "missing_keywords": "skipped_missing_keywords",
            "low_importance": "skipped_low_importance",
            "classifier_budget_rejected": "llm_budget_rejections",
            "classifier_circuit_breaker_open": "llm_circuit_breaker_rejections",
        }.get(reason)
        if key:
            source_metric[key] = int(source_metric.get(key) or 0) + 1

    def _runtime_metrics_payload(self) -> dict[str, Any]:
        drain_status = self._update_drain_state()
        protection_metrics = self._protection_data_metrics()
        return {
            "run_valid": self.run_valid,
            "run_invalid_reasons": list(self.run_invalid_reasons),
            "supervisor_entries_paused": self.supervisor_entries_paused,
            "supervisor_pause_reason": self.supervisor_pause_reason,
            "terminalization_incident_ids": sorted(
                self._terminalization_incident_ids
            ),
            "accepted_symbols": [symbol.value for symbol in self.universe.accepted_symbols],
            "enabled_strategies": [
                strategy.name.value for strategy in self.strategies if strategy.enabled
            ],
            "symbol_cycle_metrics": self.symbol_cycle_metrics,
            "strategy_evaluation_counts": self.strategy_evaluation_counts,
            "strategy_not_applicable_counts": self.strategy_not_applicable_counts,
            "signal_metrics": self._signal_metrics_snapshot(),
            "run_finalization": {
                "phase": drain_status.phase.value,
                "nominal_end_at": (
                    drain_status.nominal_end_at.isoformat()
                    if drain_status.nominal_end_at else None
                ),
                "drain_started_at": (
                    drain_status.drain_started_at.isoformat()
                    if drain_status.drain_started_at else None
                ),
                "drain_deadline_at": (
                    drain_status.drain_deadline_at.isoformat()
                    if drain_status.drain_deadline_at else None
                ),
                "timed_out": drain_status.timed_out,
                "active_execution_ids": list(
                    drain_status.active_execution_ids
                ),
            },
            "failure_occurrences": self.failure_occurrences,
            "outcome_occurrences": self.outcome_occurrences,
            "outcome_first_seen": self.outcome_first_seen,
            "outcome_classification_counts": self.outcome_classification_counts,
            "outcome_code_counts": self.outcome_code_counts,
            "critical_classification_counts": self.critical_classification_counts,
            "safety_critical_failures": int(
                self.critical_classification_counts.get(
                    RuntimeOutcome.SAFETY_CRITICAL.value, 0
                )
            ),
            "data_integrity_failures": int(
                self.critical_classification_counts.get(
                    RuntimeOutcome.DATA_INTEGRITY_CRITICAL.value, 0
                )
            ),
            "unexpected_cycle_failures": int(
                self.critical_classification_counts.get(
                    "UNEXPECTED_CYCLE_FAILURE", 0
                )
            ),
            "safe_degraded_events": int(
                self.outcome_classification_counts.get(
                    RuntimeOutcome.SAFE_DEGRADED.value, 0
                )
            ),
            "expected_rejections": int(
                self.outcome_classification_counts.get(
                    RuntimeOutcome.EXPECTED_REJECTION.value, 0
                )
            ) + sum(
                int(self.signal_metrics.get(name) or 0)
                for name in (
                    "pre_submit_rejections",
                    "portfolio_rejections",
                    "execution_policy_rejections",
                    "cooldown_rejections",
                )
            ),
            "observability_warnings": int(
                self.outcome_classification_counts.get(
                    RuntimeOutcome.OBSERVABILITY_WARNING.value, 0
                )
            ),
            "runtime_outcomes_by_code": dict(self.outcome_code_counts),
            "certification_mode": self.settings.v2_certification_mode,
            "failure_circuit_breaker_active": self.failure_circuit_breaker_active,
            "external_dependency_health": self.dependency_health.snapshot(),
            "news_metrics": self.news_metrics,
            "news_source_metrics": self.news_aggregator.source_metrics,
            "stale_metrics": {
                **self.stale_metrics,
                "critical_stale_data_incidents": self.features.stale_incidents,
                "data_age_seconds_by_source": self._data_age_metrics(),
            },
            "protection_data_metrics": protection_metrics,
            "unresolved_safe_degradations": len(
                protection_metrics.get(
                    "active_safe_degraded_executions", []
                )
            ),
            "liquidation_metrics": self._liquidation_metrics_at(
                datetime.now(timezone.utc)
            ),
            "news_primary_model": self.settings.news_primary_model,
            "news_fallback_model": self.settings.news_fallback_model,
            "last_news_model_usage": (
                self.last_news_model_usage.model_dump(mode="json")
                if self.last_news_model_usage else None
            ),
            "last_news_model_used": (
                self.last_news_model_usage.model if self.last_news_model_usage else None
            ),
            "last_news_fallback_used": (
                self.last_news_model_usage.fallback_used
                if self.last_news_model_usage else None
            ),
            "news_source_health": {
                source.name: self.news_aggregator.health[source.name].model_dump(mode="json")
                for source in self.news_aggregator.sources
            },
            "strategy_ineligible_evaluations": 0,
            "cycle_failure_repeat_limit": self.settings.v2_cycle_failure_repeat_limit,
            "cycles": self.cycles,
            "portfolio_risk": {
                "kill_switch_active": self.portfolio.kill_switch_active,
                "kill_switch_reasons": list(self.portfolio.kill_switch_reasons),
                "daily_pnl": str(getattr(self.portfolio, "daily_pnl", 0)),
                "weekly_pnl": str(getattr(self.portfolio, "weekly_pnl", 0)),
                "cumulative_realized_pnl": str(
                    getattr(self.portfolio, "cumulative_realized_pnl", 0)
                ),
                "unrealized_pnl": str(getattr(self.portfolio, "unrealized_pnl", 0)),
                "equity": str(getattr(self.portfolio, "equity", self.settings.risk_capital_usdt)),
                "peak_equity": str(getattr(self.portfolio, "peak_equity", self.settings.risk_capital_usdt)),
                "drawdown_pct": str(getattr(self.portfolio, "current_drawdown_pct", 0)),
            },
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _persist_runtime_metrics(self) -> None:
        saver = getattr(self.repository, "update_v2_run_runtime", None)
        if callable(saver):
            saver(self.run_id, self._runtime_metrics_payload())

    def _monitor_positions(
        self, cycle_id: str = "manual-position-monitor"
    ) -> None:
        prices: dict[Symbol, Decimal] = {}
        open_records: list[Any] = []
        for record in self.repository.load_demo_executions():
            if record.run_id != self.run_id or record.state.value != "DEMO_POSITION_OPEN":
                continue
            open_records.append(record)
            feature = self.features.snapshot(record.symbol)
            if feature is None:
                continue
            (
                management_price,
                management_price_at,
                management_price_received_at,
                management_price_source,
            ) = protection_management_price_input(feature)
            prices[record.symbol] = feature.last_price
            if not feature.fresh:
                self.stale_metrics["position_stale_observations"] += 1
                self.repository.save_v2_incident(V2Incident(
                    run_id=self.run_id,
                    event_type="V2_STALE_POSITION_OBSERVATION",
                    symbol=record.symbol,
                    execution_id=record.id,
                    payload={
                        "source_evidence": feature.stale_evidence,
                        "evaluation_timestamp": datetime.now(timezone.utc).isoformat(),
                        "runtime_consequence": (
                            "strategy monitoring continued; exchange protection and "
                            "reconciliation were not blocked"
                        ),
                        "classified_as": "stale_feature_warning",
                    },
                ))
            try:
                self.execution.demo_execution.monitor_strategy_position(
                    str(record.id),
                    management_price,
                    market_price_at=management_price_at,
                    market_price_received_at=management_price_received_at,
                    market_price_source=management_price_source,
                    data_fresh=feature.fresh,
                    stale_feature="; ".join(feature.stale_reasons) or None,
                    stale_age_seconds=max(
                        (
                            float(item["observed_age_seconds"])
                            for item in feature.stale_evidence
                            if item.get("observed_age_seconds") is not None
                        ),
                        default=None,
                    ),
                    stale_exit_threshold_seconds=float(
                        self.settings.v2_position_data_stale_exit_seconds
                    ),
                    setup_valid=self._position_setup_still_valid(record, feature),
                )
            except RuntimeOutcomeError as exc:
                if exc.details.classification not in NON_FAILURE_OUTCOMES:
                    raise
                self._record_non_failure_outcome(
                    exc,
                    stage="position_monitoring",
                    cycle_id=cycle_id,
                    symbol=record.symbol,
                    strategy=record.strategy_name,
                    execution_id=record.id,
                )
                continue
        marker = getattr(self.portfolio, "mark_to_market", None)
        if callable(marker):
            marker(open_records, prices)

    def _position_setup_still_valid(self, record: Any, feature: Any) -> bool:
        direction = Decimal("1") if record.side.value == "BUY" else Decimal("-1")
        momentum = direction * feature.price_momentum.get("1m", Decimal("0"))
        if momentum < -self.settings.v2_setup_invalidation_bps:
            return False
        if record.strategy_name == StrategyName.OI_FUNDING_SQUEEZE.value:
            return (
                feature.open_interest_change_pct is not None
                and feature.funding_deviation_bps is not None
            )
        if record.strategy_name == StrategyName.RANGE_MEAN_REVERSION.value:
            return feature.market_regime == "RANGE"
        return True

    def _sync_reservations(self) -> None:
        terminal = {
            "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE", "DEMO_NOT_SUBMITTED",
            "DEMO_ORDER_CANCELLED", "DEMO_CLOSED_AFTER_INTERRUPTION",
            "DEMO_CLOSED_EXTERNALLY", "DEMO_FAILED_FLAT_VERIFIED",
        }
        records = {str(item.id): item for item in self.repository.load_demo_executions()}
        for reservation in list(self.portfolio.reservations):
            if reservation.state not in self.portfolio.ACTIVE or reservation.execution_id is None:
                continue
            record = records.get(str(reservation.execution_id))
            if record and record.state.value in terminal:
                ledger = getattr(self.portfolio, "apply_execution_result", None)
                if callable(ledger):
                    ledger(record)
                self.portfolio.release(
                    reservation.id,
                    closed_at=record.closed_at or record.updated_at,
                )

    def sync_terminal_executions(self) -> None:
        """Release terminal reservations/cooldowns once and refresh cached status."""
        self._enforce_terminalization_invariants()
        self._sync_reservations()
        self._persist_runtime_metrics()
        self._refresh_status_snapshot()

    def _enforce_terminalization_invariants(self) -> dict[str, Any]:
        """Retry exact flat closes and fail the run closed after the hard bound."""
        service = self.execution.demo_execution
        retry = getattr(service, "retry_stuck_terminalizations", None)
        result = retry() if callable(retry) else {
            "retried": [], "resolved": [], "hard_failures": {},
        }
        hard_failures = dict(result.get("hard_failures") or {})
        for execution_id, blockers in hard_failures.items():
            normalized = list(dict.fromkeys(str(item) for item in blockers))
            reason = (
                f"execution {execution_id} exceeded the terminalization hard "
                f"limit: {'; '.join(normalized)}"
            )
            self.run_valid = False
            self.stop_new_entries = True
            if reason not in self.run_invalid_reasons:
                self.run_invalid_reasons.append(reason)
            if execution_id in self._terminalization_incident_ids:
                continue
            incident_id = uuid5(
                NAMESPACE_URL,
                f"bybot-v2-terminalization-hard-failure:{self.run_id}:{execution_id}",
            )
            persisted = self.repository.save_v2_incident(V2Incident(
                id=incident_id,
                run_id=self.run_id,
                event_type="V2_TERMINALIZATION_HARD_FAILURE",
                execution_id=execution_id,
                error_category="terminalization_invariant",
                payload={
                    "execution_id": execution_id,
                    "blockers": normalized,
                    "warning_threshold_seconds": (
                        self.settings.v2_terminalization_warning_seconds
                    ),
                    "hard_failure_threshold_seconds": (
                        self.settings.v2_terminalization_hard_failure_seconds
                    ),
                    "run_invalid": True,
                    "new_entries_stopped": True,
                    "reconciliation_continues": True,
                },
            ))
            if persisted is not False:
                self._terminalization_incident_ids.add(execution_id)
        return result

    def _build_status_snapshot(self) -> dict[str, Any]:
        drain_status = self._update_drain_state()
        preflight = self.execution.safety_preflight(require_auto_execution=False)
        protection_metrics = self._protection_data_metrics()
        active_reservations = [row for row in self.portfolio.reservations if row.state in self.portfolio.ACTIVE]
        accepted = [item.value for item in self.universe.accepted_symbols]
        symbols_attempted = sorted(
            symbol for symbol, metric in self.symbol_cycle_metrics.items()
            if metric.get("cycles_attempted", 0) > 0
        )
        symbols_successful = sorted(
            symbol for symbol, metric in self.symbol_cycle_metrics.items()
            if metric.get("cycles_succeeded", 0) > 0
        )
        return {
            "enabled": self.settings.v2_enabled,
            "execution_environment": "BYBIT_DEMO",
            "auto_demo_execution": self.settings.v2_auto_demo_execution,
            "run_id": self.run_id, "started_at": self.started_at.isoformat(),
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "last_error": self.last_error, "cycles": self.cycles,
            "stop_new_entries": self.stop_new_entries,
            "entries_paused": self.entries_paused,
            "supervisor_entries_paused": self.supervisor_entries_paused,
            "supervisor_pause_reason": self.supervisor_pause_reason,
            "external_dependency_health": self.dependency_health.snapshot(),
            "run_valid": self.run_valid,
            "run_invalid_reasons": list(self.run_invalid_reasons),
            "run_phase": drain_status.phase.value,
            "entries_allowed": bool(
                drain_status.entries_allowed
                and not self.entries_paused
                and self.run_valid
            ),
            "nominal_end_at": (
                drain_status.nominal_end_at.isoformat()
                if drain_status.nominal_end_at else None
            ),
            "drain_started_at": (
                drain_status.drain_started_at.isoformat()
                if drain_status.drain_started_at else None
            ),
            "drain_deadline_at": (
                drain_status.drain_deadline_at.isoformat()
                if drain_status.drain_deadline_at else None
            ),
            "seconds_until_drain": drain_status.seconds_until_drain,
            "seconds_until_nominal_end": drain_status.seconds_until_nominal_end,
            "drain_seconds_remaining": drain_status.drain_seconds_remaining,
            "drain_timed_out": drain_status.timed_out,
            "drain_active_execution_ids": list(
                drain_status.active_execution_ids
            ),
            "drain_safety_blockers": (
                [
                    "drain timeout expired with unresolved executions: "
                    + ", ".join(drain_status.active_execution_ids)
                ]
                if drain_status.timed_out else []
            ),
            "preflight_ok": not preflight, "preflight_blockers": preflight,
            "accepted_symbols": accepted,
            "rejected_symbols": [
                {"symbol": symbol.value, "reasons": status.reasons}
                for symbol, status in self.universe.statuses.items() if not status.accepted
            ],
            "strategy_flags": {row.name.value: row.enabled for row in self.strategies},
            "symbol_cycle_metrics": self.symbol_cycle_metrics,
            "strategy_evaluation_counts": self.strategy_evaluation_counts,
            "strategy_not_applicable_counts": self.strategy_not_applicable_counts,
            "total_cycle_failures": sum(self.failure_occurrences.values()),
            "unique_cycle_failure_fingerprints": len(self.failure_occurrences),
            "safety_critical_failures": int(
                self.critical_classification_counts.get(
                    RuntimeOutcome.SAFETY_CRITICAL.value, 0
                )
            ),
            "data_integrity_failures": int(
                self.critical_classification_counts.get(
                    RuntimeOutcome.DATA_INTEGRITY_CRITICAL.value, 0
                )
            ),
            "unexpected_cycle_failures": int(
                self.critical_classification_counts.get(
                    "UNEXPECTED_CYCLE_FAILURE", 0
                )
            ),
            "safe_degraded_events": int(
                self.outcome_classification_counts.get(
                    RuntimeOutcome.SAFE_DEGRADED.value, 0
                )
            ),
            "expected_rejections": int(
                self.outcome_classification_counts.get(
                    RuntimeOutcome.EXPECTED_REJECTION.value, 0
                )
            ) + sum(
                int(self.signal_metrics.get(name) or 0)
                for name in (
                    "pre_submit_rejections",
                    "portfolio_rejections",
                    "execution_policy_rejections",
                    "cooldown_rejections",
                )
            ),
            "observability_warnings": int(
                self.outcome_classification_counts.get(
                    RuntimeOutcome.OBSERVABILITY_WARNING.value, 0
                )
            ),
            "runtime_outcomes_by_code": dict(self.outcome_code_counts),
            "certification_mode": self.settings.v2_certification_mode,
            "symbols_attempted": symbols_attempted,
            "symbols_successful": symbols_successful,
            "symbols_failed": sorted(set(symbols_attempted) - set(symbols_successful)),
            "symbols_never_processed": sorted(set(accepted) - set(symbols_successful)),
            "news_metrics": self.news_metrics,
            "news_primary_model": self.settings.news_primary_model,
            "news_fallback_model": self.settings.news_fallback_model,
            "last_news_model_usage": (
                self.last_news_model_usage.model_dump(mode="json")
                if self.last_news_model_usage else None
            ),
            "last_news_model_used": (
                self.last_news_model_usage.model if self.last_news_model_usage else None
            ),
            "last_news_fallback_used": (
                self.last_news_model_usage.fallback_used
                if self.last_news_model_usage else None
            ),
            "signal_count": int(self.signal_metrics.get("admitted_signals") or 0),
            "signal_metrics": self._signal_metrics_snapshot(),
            "open_reservations": len(active_reservations),
            "max_concurrent_positions": self.settings.max_concurrent_positions,
            "kill_switch_active": self.portfolio.kill_switch_active or self.execution.demo_execution.kill_switch_active,
            "kill_switch_reasons": list(dict.fromkeys(
                self.portfolio.kill_switch_reasons + self.execution.demo_execution.kill_switch_reasons
            )),
            "terminalization_retry_warnings": (
                getattr(
                    self.execution.demo_execution,
                    "terminalization_retry_warnings", 0,
                )
            ),
            "last_terminalization_warning": (
                getattr(
                    self.execution.demo_execution,
                    "last_terminalization_warning", None,
                )
            ),
            "terminalization_hard_failures": dict(
                getattr(
                    self.execution.demo_execution,
                    "terminalization_hard_failures", {},
                )
            ),
            "websocket_reconnects": self.websocket.reconnects,
            "critical_stale_data_incidents": self.features.stale_incidents,
            "stale_data_incidents": self.features.stale_incidents,
            "stale_feature_rejections": self.stale_metrics["stale_feature_rejections"],
            "stale_rejections_by_source": self.stale_metrics["stale_rejections_by_source"],
            "stale_rejections_by_symbol": self.stale_metrics["stale_rejections_by_symbol"],
            "stale_rejections_by_strategy": self.stale_metrics["stale_rejections_by_strategy"],
            "data_age_seconds_by_source": self._data_age_metrics(),
            "protection_data_metrics": protection_metrics,
            "unresolved_safe_degradations": len(
                protection_metrics.get(
                    "active_safe_degraded_executions", []
                )
            ),
            "liquidation_strategy_metrics": self._liquidation_metrics_at(
                datetime.now(timezone.utc)
            ),
            "market_source_health": {
                key: value.model_dump(mode="json")
                for key, value in self.features.source_states.items()
            },
            "news_source_health": {
                source.name: {
                    "reliability": source.reliability,
                    "health": self.news_aggregator.health[source.name].model_dump(mode="json"),
                }
                for source in self.news_aggregator.sources
            },
            "external_trend_health": (
                self.external_trends.health.value if self.external_trends else "DISABLED"
            ),
            "persistence_status": "OK" if self.repository.available else "UNAVAILABLE",
            "status_snapshot_state": "READY",
            "status_snapshot_at": datetime.now(timezone.utc).isoformat(),
            "status_request_metrics": {
                "count": self.status_request_count,
                "failures": self.status_request_failures,
                "last_latency_ms": self.status_request_latency_ms,
            },
        }

    def _refresh_status_snapshot(self) -> None:
        try:
            snapshot = self._build_status_snapshot()
        except Exception as exc:
            with self._status_lock:
                snapshot = copy.deepcopy(self._status_snapshot)
                snapshot["status_snapshot_state"] = "STALE"
                snapshot["status_snapshot_error"] = type(exc).__name__
                snapshot["status_snapshot_at"] = datetime.now(timezone.utc).isoformat()
        with self._status_lock:
            self._status_snapshot = snapshot

    def status(self) -> dict[str, Any]:
        """Return one cached immutable-by-convention snapshot; never do I/O."""
        with self._status_lock:
            return copy.deepcopy(self._status_snapshot)

    def record_status_request(self, *, latency_ms: float, succeeded: bool) -> None:
        with self._status_lock:
            self.status_request_count += 1
            self.status_request_latency_ms = max(0.0, latency_ms)
            if not succeeded:
                self.status_request_failures += 1

    def _data_age_metrics(self) -> dict[str, Any]:
        loader = getattr(self.features, "data_age_metrics", None)
        return loader() if callable(loader) else {}

    def _protection_data_metrics(self) -> dict[str, Any]:
        loader = getattr(
            self.execution.demo_execution, "protection_data_metrics", None
        )
        return loader() if callable(loader) else {
            "state_counts": {},
            "deferred_trailing_updates": 0,
            "deferred_break_even_updates": 0,
            "deferred_adaptive_exits": 0,
            "freshness_recoveries": 0,
            "protection_freshness_hard_failures": 0,
            "active_deferred_executions": [],
        }

    def _record_signal_metric(
        self, name: str, candidate: V2SignalCandidate,
    ) -> None:
        self.signal_metrics[name] = int(self.signal_metrics.get(name) or 0) + 1
        for dimension, key in (
            ("by_strategy", candidate.strategy_name.value),
            ("by_symbol", candidate.symbol.value),
        ):
            rows = self.signal_metrics.setdefault(dimension, {})
            metrics = rows.setdefault(key, {})
            metrics[name] = int(metrics.get(name) or 0) + 1

    def _signal_metrics_snapshot(self) -> dict[str, Any]:
        metrics = copy.deepcopy(self.signal_metrics)
        executions = [
            item for item in self.repository.load_demo_executions()
            if item.run_id == self.run_id
        ]
        metrics["strategy_evaluations"] = sum(
            int(value) for value in self.strategy_evaluation_counts.values()
        )
        metrics["orders_submitted"] = sum(bool(item.order_id) for item in executions)
        metrics["orders_filled"] = sum(item.accepted_quantity > 0 for item in executions)
        metrics["risk_rejections"] = max(
            int(metrics.get("risk_rejections") or 0),
            sum(item.state == "EXECUTION_BLOCKED" for item in self.candidates),
        )
        metrics["persistence_rejections"] = max(
            int(metrics.get("persistence_rejections") or 0),
            sum(item.state == "PERSISTENCE_BLOCKED" for item in self.candidates),
        )
        metrics["completed_trades"] = sum(
            item.state.value in {
                "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE",
                "DEMO_CLOSED_AFTER_INTERRUPTION", "DEMO_CLOSED_EXTERNALLY",
                "DEMO_FAILED_FLAT_VERIFIED",
            }
            for item in executions
        )
        return metrics

    def finish(self) -> dict[str, Any]:
        self.stop_new_entries = True
        self._persist_runtime_metrics()
        report = self.reporter.generate(self.run_id)
        report["run_valid"] = self.run_valid
        report["run_invalid_reasons"] = list(self.run_invalid_reasons)
        if not self.run_valid:
            report["functional_result"] = "FAIL"
            blockers = list(report.get("functional_blockers") or [])
            blockers.extend(self.run_invalid_reasons)
            report["functional_blockers"] = list(dict.fromkeys(blockers))
        self.repository.finish_v2_run(self.run_id, report)
        for executor in self._execution_pools.values():
            executor.shutdown(wait=False, cancel_futures=False)
        return report

    def begin_draining(self) -> dict[str, Any]:
        self.stop_new_entries = True
        status = self.drain.force_draining()
        self._persist_runtime_metrics()
        self._refresh_status_snapshot()
        return {
            "run_id": self.run_id,
            "run_phase": status.phase.value,
            "new_entries_stopped": True,
            "existing_position_management_active": True,
        }

    def set_supervisor_entries_paused(
        self, paused: bool, *, reason: str | None = None,
    ) -> dict[str, Any]:
        """Pause admission without changing the durable run phase."""
        self.supervisor_entries_paused = bool(paused)
        self.supervisor_pause_reason = reason if paused else None
        self._persist_runtime_metrics()
        self._refresh_status_snapshot()
        return {
            "run_id": self.run_id,
            "run_phase": self.drain.phase.value,
            "supervisor_entries_paused": self.supervisor_entries_paused,
            "supervisor_pause_reason": self.supervisor_pause_reason,
            "existing_position_management_active": True,
        }

    def execution_entries_allowed(self) -> bool:
        return bool(
            self.drain.phase == V2RunPhase.RUNNING
            and not self.entries_paused
            and self.run_valid
        )

    @property
    def entries_paused(self) -> bool:
        return bool(
            self.stop_new_entries
            or self.dependency_health.entries_paused
            or self.supervisor_entries_paused
        )

    def _restore_finalization_on_startup(self) -> None:
        if self.drain.phase != V2RunPhase.RUNNING:
            self.stop_new_entries = True
            self._enforce_terminalization_invariants()
            self._sync_reservations()
        status = self._update_drain_state()
        remote = self._remote_finalization_snapshot()
        self._persist_runtime_metrics()
        incident = V2Incident(
            id=uuid5(
                NAMESPACE_URL,
                (
                    f"bybot-v2-phase-restore:{self.run_id}:"
                    f"{self._persisted_phase.value}:"
                    f"{self._initial_restored_phase.value}:"
                    f"{self._restored_runtime_updated_at or 'initial'}"
                ),
            ),
            run_id=self.run_id,
            event_type="V2_RUN_PHASE_RESTORED",
            payload={
                "persisted_phase": self._persisted_phase.value,
                "initial_restored_phase": self._initial_restored_phase.value,
                "restored_phase": status.phase.value,
                "run_id": self.run_id,
                "nominal_end_at": (
                    status.nominal_end_at.isoformat()
                    if status.nominal_end_at else None
                ),
                "drain_started_at": (
                    status.drain_started_at.isoformat()
                    if status.drain_started_at else None
                ),
                "active_executions": list(status.active_execution_ids),
                "unresolved_executions": remote["unresolved_executions"],
                "remote_positions": remote["remote_positions"],
                "remote_orders": remote["remote_orders"],
                "remote_state_authoritative": remote[
                    "remote_state_authoritative"
                ],
                "entries_enabled": self.execution_entries_allowed(),
                "finalization_result": (
                    "FINISHED"
                    if status.phase == V2RunPhase.FINISHED
                    else "WAITING_FOR_AUTHORITATIVE_FLAT_STATE"
                    if status.phase != V2RunPhase.RUNNING
                    else "RUNNING_RESTORED"
                ),
            },
        )
        self.repository.save_v2_incident(incident)

    def _active_execution_ids(self) -> list[str]:
        terminal = {
            "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE", "DEMO_FAILED",
            "DEMO_NOT_SUBMITTED", "DEMO_ORDER_CANCELLED",
            "DEMO_CLOSED_AFTER_INTERRUPTION", "DEMO_CLOSED_EXTERNALLY",
            "DEMO_FAILED_FLAT_VERIFIED",
        }
        return [
            str(item.id)
            for item in self.repository.load_demo_executions()
            if item.run_id == self.run_id and item.state.value not in terminal
        ]

    def _remote_finalization_snapshot(self) -> dict[str, Any]:
        active = self._active_execution_ids()
        status_loader = getattr(self.execution.demo_execution, "as_status", None)
        if not callable(status_loader):
            return {
                "remote_state_authoritative": not active,
                "remote_positions": 0 if not active else None,
                "remote_orders": 0 if not active else None,
                "unresolved_executions": len(active),
                "ready": not active,
            }
        status = status_loader()
        remote_positions = int(status.get("bot_owned_open_positions") or 0)
        remote_orders = (
            int(status.get("bot_owned_open_orders") or 0)
            + int(status.get("unrelated_open_orders") or 0)
        )
        authoritative = bool(status.get("remote_state_authoritative"))
        return {
            "remote_state_authoritative": authoritative,
            "remote_positions": remote_positions,
            "remote_orders": remote_orders,
            "unresolved_executions": len(active),
            "ready": bool(
                authoritative
                and not active
                and remote_positions == 0
                and remote_orders == 0
            ),
        }

    def _update_drain_state(self) -> Any:
        active = self._active_execution_ids()
        remote = self._remote_finalization_snapshot()
        status = self.drain.evaluate(
            active_execution_ids=active,
            finalization_ready=(
                bool(remote["ready"])
                if self.drain.phase != V2RunPhase.RUNNING else False
            ),
        )
        if status.phase != V2RunPhase.RUNNING:
            self.stop_new_entries = True
        return status


async def v2_cycle_loop(runtime: V2Runtime, interval_seconds: int = 5) -> None:
    while True:
        runtime.refresh_universe_if_due()
        await runtime.cycle()
        await asyncio.sleep(interval_seconds)


def _aware_utc_datetime(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    elif value:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    else:
        stamp = datetime.now(timezone.utc)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("event timestamp must be timezone-aware")
    return stamp.astimezone(timezone.utc)


def _candidate_signature(candidate: V2SignalCandidate) -> str:
    components = candidate.score_components
    material = "|".join((
        candidate.strategy_name.value,
        candidate.symbol.value,
        candidate.side.value,
        str(candidate.raw_strategy_score),
        str(components.final_score if components else ""),
        str(candidate.threshold),
        str(candidate.estimated_edge_bps),
        str(candidate.rejection_reason or ""),
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _news_skip_reason(
    *, accepted: bool, filter_reason: str,
    classification: Any | None,
    before: dict[str, Any], after: dict[str, Any],
) -> str:
    if not accepted:
        return filter_reason
    if classification is None:
        return "codex_importance_below_minimum"
    return _news_classification_reason(classification, False, before, after)


def _news_classification_reason(
    classification: Any,
    llm_used: bool,
    before: dict[str, Any],
    after: dict[str, Any],
) -> str:
    error_code = str(getattr(classification, "error_code", None) or "")
    if error_code in {
        "HOURLY_REQUEST_BUDGET", "DAILY_REQUEST_BUDGET", "DAILY_TOKEN_BUDGET",
    }:
        return "classifier_budget_rejected"
    if error_code == "CIRCUIT_OPEN":
        return "classifier_circuit_breaker_open"
    if error_code:
        return f"classifier_failed:{error_code.lower()}"
    if bool(getattr(classification, "cache_hit", False)) or int(
        after.get("llm_cache_hits") or 0
    ) > int(before.get("llm_cache_hits") or 0):
        return "classifier_cache_hit"
    if str(getattr(classification, "provider_name", "")) == "deterministic-v2":
        return "deterministic_high_confidence"
    return "llm_requested" if llm_used else "classifier_completed_without_provider_call"


def _sanitize_runtime_error(message: str) -> str:
    text = " ".join(message.split())
    text = re.sub(r"https?://[^\s?]+\?\S+", "[REDACTED_URL]", text)
    text = re.sub(
        r"(?i)(api[_-]?key|secret|signature|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]", text,
    )
    return text[:500] or "exception message was empty"


def _failure_fingerprint(
    exc: Exception, *, stage: str, symbol: Symbol | None, strategy: str | None
) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    location = "|".join(
        f"{frame.name}:{frame.lineno}" for frame in frames[-4:]
    )
    material = (
        f"{type(exc).__name__}|{stage}|{symbol.value if symbol else ''}|"
        f"{strategy or ''}|{_sanitize_runtime_error(str(exc))}|{location}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _input_field_for_exception(exc: Exception) -> str | None:
    message = str(exc)
    for field in (
        "lastPrice", "spread_bps", "bid_price", "ask_price", "volume24h",
        "fundingRate", "openInterest", "feature_snapshot",
    ):
        if field.casefold() in message.casefold():
            return field
    if "required market decimal missing" in message:
        return "lastPrice"
    return None


def _is_transient_failure(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, asyncio.TimeoutError))
