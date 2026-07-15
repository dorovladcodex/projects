from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.models import Symbol
from app.news.service import NewsService
from app.v2.analytics import V2ReportGenerator
from app.v2.execution import V2ExecutionCoordinator
from app.v2.market import (
    BybitPublicWebSocketEngine, BybitRestMetricsPoller, RollingFeatureEngine,
)
from app.v2.logging import configure_v2_logging
from app.v2.models import StrategyName, V2Incident, V2SignalCandidate
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

    def start(self) -> None:
        if not self.settings.v2_enabled:
            return
        if not self.repository.begin_v2_run(self.run_id, self.started_at):
            raise RuntimeError("V2 run boundary could not be persisted")
        self.universe.refresh()

    async def cycle(self) -> None:
        if not self.settings.v2_enabled:
            return
        try:
            now = datetime.now(timezone.utc)
            if self._last_rest_poll_at is None or (
                now - self._last_rest_poll_at
            ).total_seconds() >= self.settings.v2_rest_metrics_interval_seconds:
                await asyncio.to_thread(
                    self.rest_metrics.poll, self.universe.accepted_symbols
                )
                self._last_rest_poll_at = now
                if self.external_trends is not None:
                    await self.external_trends.poll()
            await self._process_news()
            await self._process_market_strategies()
            self._sync_reservations()
            self._monitor_positions()
            self.last_error = None
        except Exception as exc:
            self.last_error = type(exc).__name__
            self.logger.error(
                "V2 cycle failed", extra={
                    "run_id": self.run_id, "event_type": "V2_CYCLE_FAILURE",
                    "execution_environment": "BYBIT_DEMO",
                    "error_category": type(exc).__name__,
                },
            )
            self.repository.save_v2_incident(V2Incident(
                run_id=self.run_id, event_type="V2_CYCLE_FAILURE",
                error_category=type(exc).__name__,
            ))
        self.cycles += 1
        self.last_cycle_at = datetime.now(timezone.utc)

    def refresh_universe_if_due(self) -> None:
        now = datetime.now(timezone.utc)
        if self.universe.last_refresh_at is None or (
            now - self.universe.last_refresh_at
        ).total_seconds() >= self.settings.v2_universe_refresh_seconds:
            self.universe.refresh(now=now)

    async def _process_news(self) -> None:
        executable: list[V2SignalCandidate] = []
        for item, symbols, _fingerprint in await self.news_aggregator.poll():
            accepted, _, classification = self.v1_news_service.ingest(item)
            if not accepted or classification is None or not classification.trade_eligible:
                continue
            for symbol in symbols:
                feature = self._feature(symbol)
                if feature is None:
                    continue
                strategy = next(row for row in self.strategies if row.name == StrategyName.NEWS_MOMENTUM_V2)
                if not strategy.enabled:
                    continue
                context = NewsStrategyContext(
                    sentiment=classification.sentiment.value,
                    confidence=Decimal(str(classification.confidence)),
                    importance=Decimal(str(item.importance)),
                    news_ids=(str(item.id),),
                    market_wide=len(symbols) > 1,
                )
                candidate = self._admit(strategy.evaluate(feature, news=context))
                if candidate.admitted:
                    executable.append(candidate)
        await self._execute_concurrently(executable)

    async def _process_market_strategies(self) -> None:
        if self.stop_new_entries:
            return
        executable: list[V2SignalCandidate] = []
        for symbol in self.universe.accepted_symbols:
            feature = self._feature(symbol)
            if feature is None:
                continue
            for strategy in self.strategies:
                if not strategy.enabled or strategy.name == StrategyName.NEWS_MOMENTUM_V2:
                    continue
                if strategy.name == StrategyName.MEME_TREND:
                    candidate = strategy.evaluate(feature, meme=MemeTrendContext(
                        Decimal(str(self.external_trends.score(symbol)))
                        if self.external_trends else Decimal("0")
                    ))
                else:
                    candidate = strategy.evaluate(feature)
                admitted = self._admit(candidate)
                if admitted.admitted:
                    executable.append(admitted)
        await self._execute_concurrently(executable)

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
        self.repository.save_v2_signal_candidate(candidate)
        self.candidates.append(candidate)
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

    async def _execute_concurrently(self, candidates: list[V2SignalCandidate]) -> None:
        if not self.settings.v2_auto_demo_execution or self.stop_new_entries:
            return
        await asyncio.gather(*(
            asyncio.to_thread(self.execution.execute, candidate)
            for candidate in candidates
        ))

    def _monitor_positions(self) -> None:
        for record in self.repository.load_demo_executions():
            if record.run_id != self.run_id or record.state.value != "DEMO_POSITION_OPEN":
                continue
            feature = self.features.snapshot(record.symbol)
            if feature is None:
                continue
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
        return {
            "enabled": self.settings.v2_enabled,
            "execution_environment": "BYBIT_DEMO",
            "auto_demo_execution": self.settings.v2_auto_demo_execution,
            "run_id": self.run_id, "started_at": self.started_at.isoformat(),
            "last_cycle_at": self.last_cycle_at.isoformat() if self.last_cycle_at else None,
            "last_error": self.last_error, "cycles": self.cycles,
            "stop_new_entries": self.stop_new_entries,
            "preflight_ok": not preflight, "preflight_blockers": preflight,
            "accepted_symbols": [item.value for item in self.universe.accepted_symbols],
            "rejected_symbols": [
                {"symbol": symbol.value, "reasons": status.reasons}
                for symbol, status in self.universe.statuses.items() if not status.accepted
            ],
            "strategy_flags": {row.name.value: row.enabled for row in self.strategies},
            "signal_count": len(self.candidates),
            "open_reservations": len(active_reservations),
            "max_concurrent_positions": self.settings.max_concurrent_positions,
            "kill_switch_active": self.portfolio.kill_switch_active or self.execution.demo_execution.kill_switch_active,
            "kill_switch_reasons": list(dict.fromkeys(
                self.portfolio.kill_switch_reasons + self.execution.demo_execution.kill_switch_reasons
            )),
            "websocket_reconnects": self.websocket.reconnects,
            "stale_data_incidents": self.features.stale_incidents,
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

    def finish(self) -> dict[str, Any]:
        self.stop_new_entries = True
        report = self.reporter.generate(self.run_id)
        self.repository.finish_v2_run(self.run_id, report)
        return report


async def v2_cycle_loop(runtime: V2Runtime, interval_seconds: int = 5) -> None:
    while True:
        runtime.refresh_universe_if_due()
        await runtime.cycle()
        await asyncio.sleep(interval_seconds)
