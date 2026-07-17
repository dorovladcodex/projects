from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import logging
import re
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.config import Settings
from app.models import Symbol
from app.news.service import NewsService
from app.v2.analytics import V2ReportGenerator
from app.v2.execution import V2ExecutionCoordinator
from app.v2.market import (
    BybitPublicWebSocketEngine, BybitRestMetricsPoller, RollingFeatureEngine,
)
from app.v2.logging import configure_v2_logging
from app.v2.models import NewsModelUsage, StrategyName, V2Incident, V2SignalCandidate
from app.v2.news import V2ExternalTrendService, V2NewsAggregator
from app.v2.portfolio import PortfolioRiskService, correlation_group
from app.v2.scoring import AdmissionContext, CommonScoringPipeline
from app.v2.strategies import MemeTrendContext, NewsStrategyContext, build_v2_strategies
from app.v2.universe import SymbolUniverseService


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
        self.started_at = datetime.now(timezone.utc)
        self.last_cycle_at: datetime | None = None
        self.last_error: str | None = None
        self.stop_new_entries = False
        self.cycles = 0
        self._last_rest_poll_at: datetime | None = None
        self._execution_pools: dict[Symbol, ThreadPoolExecutor] = {}
        restored = repository.load_v2_run_runtime(run_id) if hasattr(repository, "load_v2_run_runtime") else {}
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
        self.failure_occurrences: dict[str, int] = dict(
            restored.get("failure_occurrences") or {}
        )
        self.news_metrics: dict[str, int] = {
            "items_received": 0, "raw_news_feed_items_received": 0,
            "unique_news_items_discovered": 0, "items_deduplicated": 0,
            "items_rejected_by_deterministic_filter": 0,
            "items_sent_to_llm": 0, "llm_classifications": 0,
            "trade_eligible_items": 0, "mapped_assets": 0,
            "news_momentum_candidates_generated": 0,
            "news_momentum_candidates_admitted": 0,
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

    def start(self) -> None:
        if not self.settings.v2_enabled:
            return
        if not self.repository.begin_v2_run(self.run_id, self.started_at):
            raise RuntimeError("V2 run boundary could not be persisted")
        self.universe.refresh()

    async def cycle(self) -> None:
        if not self.settings.v2_enabled:
            return
        cycle_id = f"{self.run_id}:{self.cycles + 1}"
        cycle_failures_before = sum(self.failure_occurrences.values())
        now = datetime.now(timezone.utc)
        if self._last_rest_poll_at is None or (
            now - self._last_rest_poll_at
        ).total_seconds() >= self.settings.v2_rest_metrics_interval_seconds:
            try:
                await asyncio.to_thread(
                    self.rest_metrics.poll, self.universe.accepted_symbols
                )
                self._last_rest_poll_at = now
                if self.external_trends is not None:
                    await self.external_trends.poll()
            except Exception as exc:
                self._record_failure(exc, stage="rest_metrics", cycle_id=cycle_id)
        # News has an independent cadence and may include slow RSS/LLM work.
        # It must never serialize market admission or execution dispatch.
        news_task = asyncio.create_task(self._process_news(cycle_id))
        await self._process_market_strategies(cycle_id)
        try:
            await news_task
        except Exception as exc:
            self._record_failure(exc, stage="news_pipeline", cycle_id=cycle_id)
        try:
            self._sync_reservations()
            self._monitor_positions()
        except Exception as exc:
            self._record_failure(exc, stage="position_monitoring", cycle_id=cycle_id)
        self.cycles += 1
        self.last_cycle_at = datetime.now(timezone.utc)
        self.last_error = (
            "cycle contained isolated failures"
            if sum(self.failure_occurrences.values()) > cycle_failures_before else None
        )
        self._persist_runtime_metrics()

    def refresh_universe_if_due(self) -> None:
        now = datetime.now(timezone.utc)
        if self.universe.last_refresh_at is None or (
            now - self.universe.last_refresh_at
        ).total_seconds() >= self.settings.v2_universe_refresh_seconds:
            self.universe.refresh(now=now)

    async def _process_news(self, cycle_id: str) -> None:
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
            decision: dict[str, Any] = {
                "news_id": str(item.id), "llm_used": False, "model": None,
                "sentiment": None, "importance": item.importance,
                "urgency": None, "confidence": None,
                "mapped_symbols": [symbol.value for symbol in symbols],
                "market_confirmation_result": {}, "candidate_ids": [],
                "final_decision": "NO_CANDIDATE", "rejection_reason": None,
            }
            try:
                before_calls = self.v1_news_service.real_llm_calls_count
                accepted, filter_reason, classification = await asyncio.to_thread(
                    self.v1_news_service.ingest, item
                )
                decision["deterministic_filter_decision"] = filter_reason
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
            self.news_metrics["llm_classifications"] += 1
            if decision["llm_used"]:
                self.news_metrics["items_sent_to_llm"] += 1
            source_metric = self.news_aggregator.source_metrics.get(item.source)
            if source_metric is not None:
                source_metric["classified_items"] = source_metric.get("classified_items", 0) + 1
                source_metric["items_sent_to_llm"] = source_metric.get("items_sent_to_llm", 0) + int(decision["llm_used"])
            if not classification.trade_eligible:
                decision["rejection_reason"] = "classification_not_trade_eligible"
                self._persist_news_audit("V2_NEWS_DECISION_AUDIT", decision)
                continue
            self.news_metrics["trade_eligible_items"] += 1
            if source_metric is not None:
                source_metric["trade_eligible_items"] = source_metric.get("trade_eligible_items", 0) + 1
            for symbol in symbols:
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
            if not decision["candidate_ids"] and not decision["rejection_reason"]:
                decision["rejection_reason"] = "no_accepted_symbol_with_fresh_market_confirmation"
            self._persist_news_audit("V2_NEWS_DECISION_AUDIT", decision)
        await self._execute_concurrently(executable, cycle_id)

    async def _process_market_strategies(self, cycle_id: str) -> None:
        if self.stop_new_entries:
            return
        dispatches: list[tuple[V2SignalCandidate, asyncio.Future[Any]]] = []
        for symbol in self.universe.accepted_symbols:
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
                        candidate = strategy.evaluate(feature, meme=MemeTrendContext(
                            Decimal(str(self.external_trends.score(symbol)))
                            if self.external_trends else Decimal("0")
                        ))
                    else:
                        candidate = strategy.evaluate(feature)
                    admitted = self._admit(candidate)
                    metric["candidates_generated"] += 1
                    if admitted.admitted:
                        metric["candidates_admitted"] += 1
                        dispatched = self._dispatch_now(admitted)
                        if dispatched is not None:
                            dispatches.append((admitted, dispatched))
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
            if symbol_failed:
                metric["cycles_failed"] += 1
            else:
                metric["cycles_succeeded"] += 1
                metric["latest_success_timestamp"] = datetime.now(timezone.utc).isoformat()
                metric["latest_failure_category"] = None
        await self._collect_dispatches(dispatches, cycle_id)

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
        notional = self.settings.v2_target_notional_for_symbol(candidate.symbol.value)
        portfolio_reasons = self.portfolio.block_reasons(candidate.symbol, notional)
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
        candidate.candidate_persisted_at = datetime.now(timezone.utc)
        self.repository.save_v2_signal_candidate(candidate)
        self.candidates.append(candidate)
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

    async def _execute_concurrently(
        self, candidates: list[V2SignalCandidate], cycle_id: str
    ) -> None:
        if not self.settings.v2_auto_demo_execution or self.stop_new_entries:
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
        if not self.settings.v2_auto_demo_execution or self.stop_new_entries:
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

    def _liquidation_not_applicable_reason(
        self, feature: Any,
    ) -> str | None:
        if not feature.liquidation_data_valid:
            return "liquidation_data_invalid"
        if not feature.liquidation_feed_initialized:
            return "liquidation_feed_never_initialized"
        if not feature.liquidation_feed_available:
            return "liquidation_feed_unavailable"
        if (
            feature.liquidation_data_age_seconds is None
            or feature.liquidation_data_age_seconds
            > self.settings.v2_liquidation_stale_seconds
        ):
            return "liquidation_feed_stale"
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
        message = _sanitize_runtime_error(str(exc))
        fingerprint = _failure_fingerprint(
            exc, stage=stage, symbol=symbol, strategy=strategy
        )
        count = self.failure_occurrences.get(fingerprint, 0) + 1
        self.failure_occurrences[fingerprint] = count
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
        }
        incident = V2Incident(
            id=uuid5(NAMESPACE_URL, f"bybot-v2-failure:{self.run_id}:{fingerprint}"),
            run_id=self.run_id, event_type="V2_CYCLE_FAILURE", symbol=symbol,
            error_category=type(exc).__name__, payload=payload, occurred_at=now,
        )
        self.repository.save_v2_incident(incident)
        self.logger.exception(
            "V2 isolated failure: %s", message,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "run_id": self.run_id, "strategy": strategy,
                "symbol": symbol.value if symbol else None,
                "event_type": "V2_CYCLE_FAILURE",
                "execution_environment": "BYBIT_DEMO",
                "error_category": type(exc).__name__,
                "processing_stage": stage, "source": source,
                "traceback_fingerprint": fingerprint, "cycle_id": cycle_id,
            },
        )

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

    def _runtime_metrics_payload(self) -> dict[str, Any]:
        return {
            "accepted_symbols": [symbol.value for symbol in self.universe.accepted_symbols],
            "enabled_strategies": [
                strategy.name.value for strategy in self.strategies if strategy.enabled
            ],
            "symbol_cycle_metrics": self.symbol_cycle_metrics,
            "strategy_evaluation_counts": self.strategy_evaluation_counts,
            "strategy_not_applicable_counts": self.strategy_not_applicable_counts,
            "failure_occurrences": self.failure_occurrences,
            "news_metrics": self.news_metrics,
            "news_source_metrics": self.news_aggregator.source_metrics,
            "stale_metrics": {
                **self.stale_metrics,
                "critical_stale_data_incidents": self.features.stale_incidents,
                "data_age_seconds_by_source": self._data_age_metrics(),
            },
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
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _persist_runtime_metrics(self) -> None:
        saver = getattr(self.repository, "update_v2_run_runtime", None)
        if callable(saver):
            saver(self.run_id, self._runtime_metrics_payload())

    def _monitor_positions(self) -> None:
        for record in self.repository.load_demo_executions():
            if record.run_id != self.run_id or record.state.value != "DEMO_POSITION_OPEN":
                continue
            feature = self.features.snapshot(record.symbol)
            if feature is None:
                continue
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
            self.execution.demo_execution.monitor_strategy_position(
                str(record.id), feature.last_price, data_fresh=feature.fresh,
            )

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
                self.portfolio.release(reservation.id, closed_at=record.updated_at)

    def status(self) -> dict[str, Any]:
        preflight = self.execution.safety_preflight(require_auto_execution=False)
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
            "signal_count": len(self.candidates),
            "open_reservations": len(active_reservations),
            "max_concurrent_positions": self.settings.max_concurrent_positions,
            "kill_switch_active": self.portfolio.kill_switch_active or self.execution.demo_execution.kill_switch_active,
            "kill_switch_reasons": list(dict.fromkeys(
                self.portfolio.kill_switch_reasons + self.execution.demo_execution.kill_switch_reasons
            )),
            "websocket_reconnects": self.websocket.reconnects,
            "critical_stale_data_incidents": self.features.stale_incidents,
            "stale_data_incidents": self.features.stale_incidents,
            "stale_feature_rejections": self.stale_metrics["stale_feature_rejections"],
            "stale_rejections_by_source": self.stale_metrics["stale_rejections_by_source"],
            "stale_rejections_by_symbol": self.stale_metrics["stale_rejections_by_symbol"],
            "stale_rejections_by_strategy": self.stale_metrics["stale_rejections_by_strategy"],
            "data_age_seconds_by_source": self._data_age_metrics(),
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
        }

    def _data_age_metrics(self) -> dict[str, Any]:
        loader = getattr(self.features, "data_age_metrics", None)
        return loader() if callable(loader) else {}

    def finish(self) -> dict[str, Any]:
        self.stop_new_entries = True
        self._persist_runtime_metrics()
        report = self.reporter.generate(self.run_id)
        self.repository.finish_v2_run(self.run_id, report)
        for executor in self._execution_pools.values():
            executor.shutdown(wait=False, cancel_futures=False)
        return report


async def v2_cycle_loop(runtime: V2Runtime, interval_seconds: int = 5) -> None:
    while True:
        runtime.refresh_universe_if_due()
        await runtime.cycle()
        await asyncio.sleep(interval_seconds)


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
