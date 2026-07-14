from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from uuid import NAMESPACE_URL, UUID, uuid5

from app.bybit.market_data import MarketDataService
from app.bybit.private import BybitAccountService
from app.config import BotMode, ExecutionMode, Settings
from app.models import (
    Asset,
    CandidateLifecycleState,
    ClassificationStatus,
    ExecutionEnvironment,
    MarketConfirmation,
    MarketSnapshot,
    NewsClassification,
    NewsItem,
    NewsSignalAction,
    NewsSignalCandidate,
    RiskDecision,
    RiskContext,
    Sentiment,
    Side,
    SignalAction,
    SignalDryRunResult,
    SignalEvaluation,
    SignalRiskPreview,
    SimpleTrend,
    Symbol,
    TradeSignal,
)
from app.news.service import NewsService
from app.news.eligibility import calculate_trade_eligibility
from app.portfolio.paper_trading import PaperTradingService
from app.risk import RiskManager, RiskRules
from app.db.persistence import PersistenceRepository


class SignalCandidateService:
    """Maintain dry-run candidates and re-evaluate them without execution."""

    def __init__(
        self,
        settings: Settings,
        news_service: NewsService,
        market_data: MarketDataService,
        account_service: BybitAccountService,
        paper_trading: PaperTradingService,
        repository: PersistenceRepository | None = None,
        demo_execution: object | None = None,
    ) -> None:
        self.settings = settings
        self.news_service = news_service
        self.market_data = market_data
        self.account_service = account_service
        self.paper_trading = paper_trading
        self.repository = repository
        self.demo_execution = demo_execution
        self.processed_news_ids: set[UUID] = set()
        self.results: list[SignalDryRunResult] = []
        self.risk_preview_approved_count = 0
        self.risk_preview_blocked_count = 0
        self.last_signal_evaluation_at: datetime | None = None
        self._registry_lock = RLock()
        self._candidate_locks: dict[UUID, RLock] = {}
        self._evaluation_snapshots: dict[UUID, MarketSnapshot] = {}

    def restore(self, *, now: datetime | None = None) -> None:
        if not self.repository or not self.repository.available:
            return
        now = now or datetime.now(timezone.utc)
        environment = (
            ExecutionEnvironment.BYBIT_DEMO
            if self.settings.execution_mode == ExecutionMode.BYBIT_DEMO
            else ExecutionEnvironment.PAPER
        )
        self.results = self.repository.load_signal_results(environment)
        self.processed_news_ids = {result.candidate.news_id for result in self.results}
        for result in self.results:
            self._candidate_locks[result.candidate.id] = RLock()
            if (
                result.candidate.state == CandidateLifecycleState.PENDING_CONFIRMATION
                and now >= result.candidate.expires_at
            ):
                result.candidate.state = CandidateLifecycleState.EXPIRED
                result.candidate.final_action = NewsSignalAction.NO_TRADE
                result.candidate.reasons = [
                    "market confirmation was not received before signal expiry"
                ]
                self.repository.save_signal_result(result)

    @property
    def candidates(self) -> list[NewsSignalCandidate]:
        with self._registry_lock:
            results = list(self.results)
        return [self._candidate_snapshot(result) for result in results]

    @property
    def last_result(self) -> SignalDryRunResult | None:
        with self._registry_lock:
            result = self.results[-1] if self.results else None
        return self._result_snapshot(result) if result else None

    @property
    def no_trade_candidates_count(self) -> int:
        return sum(
            item.state in {CandidateLifecycleState.BLOCKED, CandidateLifecycleState.EXPIRED}
            and item.final_action == NewsSignalAction.NO_TRADE
            for item in self.candidates
        )

    def state_count(self, state: CandidateLifecycleState) -> int:
        return sum(item.state == state for item in self.candidates)

    def process_pending(self) -> list[SignalDryRunResult]:
        created: list[SignalDryRunResult] = []
        for classification in self.news_service.classifications:
            if classification.news_id not in self.processed_news_ids:
                created.extend(self.process_news_id(classification.news_id))
        return created

    def process_news_id(
        self,
        news_id: UUID,
        *,
        allow_reprocess: bool = False,
        now: datetime | None = None,
    ) -> list[SignalDryRunResult]:
        with self._registry_lock:
            if news_id in self.processed_news_ids:
                if not allow_reprocess:
                    return [
                        result for result in self.results
                        if result.candidate.news_id == news_id
                    ]
                if not self.settings.test_mode:
                    raise PermissionError("explicit signal reprocessing requires TEST_MODE=true")

            news, classification = self._find_news_and_classification(news_id)
            eligibility = calculate_trade_eligibility(
                classification_status=classification.classification_status,
                sentiment=classification.sentiment,
                confidence=classification.confidence,
                asset=classification.asset,
                category=classification.category,
                error_code=classification.error_code,
                minimum_confidence=self.settings.signal_min_classification_confidence,
            )
            if (
                classification.classification_status
                not in {ClassificationStatus.SUCCESS, ClassificationStatus.CACHE_HIT}
                or not classification.trade_eligible
                or not eligibility.trade_eligible
                or classification.sentiment == Sentiment.NEUTRAL
            ):
                self.processed_news_ids.add(news_id)
                return []
            symbols = _symbols_for_asset(classification.asset)
            new_results = [
                self._new_result(news, classification, symbol, now=now) for symbol in symbols
            ] or [self._new_result(news, classification, None, now=now)]
            for result in new_results:
                self._candidate_locks[result.candidate.id] = RLock()
            self.results.extend(new_results)
            self.processed_news_ids.add(news_id)
            if self.repository:
                for result in new_results:
                    self.repository.save_signal_result(result)
            return new_results

    def reevaluate_pending(self, *, now: datetime | None = None) -> list[SignalDryRunResult]:
        now = now or datetime.now(timezone.utc)
        updated: list[SignalDryRunResult] = []
        with self._registry_lock:
            results = list(self.results)
        for result in results:
            if self._try_evaluate_pending(result, now):
                updated.append(result)
        return updated

    def recheck_candidate(
        self, candidate_id: UUID, *, now: datetime | None = None
    ) -> SignalDryRunResult:
        result = self.get_result(candidate_id)
        lock = self._lock_for(candidate_id)
        with lock:
            if result.candidate.state == CandidateLifecycleState.PENDING_CONFIRMATION:
                news, classification = self._find_news_and_classification(
                    result.candidate.news_id
                )
                self._evaluate(
                    result, news, classification, now or datetime.now(timezone.utc)
                )
        return result

    def recheck_candidate_with_snapshot(
        self,
        candidate_id: UUID,
        snapshot: MarketSnapshot,
        *,
        volume_change_pct: float | None,
        volume_spike: bool | None,
        now: datetime | None = None,
    ) -> SignalDryRunResult:
        result = self.get_result(candidate_id)
        lock = self._lock_for(candidate_id)
        with lock:
            if result.candidate.state == CandidateLifecycleState.PENDING_CONFIRMATION:
                news, classification = self._find_news_and_classification(
                    result.candidate.news_id
                )
                self._evaluate(
                    result,
                    news,
                    classification,
                    now or datetime.now(timezone.utc),
                    snapshot_override=snapshot,
                    volume_change_override=volume_change_pct,
                    volume_spike_override=volume_spike,
                )
        return result

    def get_result(self, candidate_id: UUID) -> SignalDryRunResult:
        with self._registry_lock:
            result = next(
                (item for item in self.results if item.candidate.id == candidate_id), None
            )
        if result is None:
            raise ValueError("signal candidate not found")
        return result

    def _try_evaluate_pending(
        self, result: SignalDryRunResult, now: datetime
    ) -> bool:
        lock = self._lock_for(result.candidate.id)
        with lock:
            candidate = result.candidate
            if candidate.state != CandidateLifecycleState.PENDING_CONFIRMATION:
                return False
            if candidate.evaluation_history:
                last_evaluated_at = candidate.evaluation_history[-1].evaluated_at
                interval = timedelta(
                    seconds=self.settings.signal_reevaluation_interval_seconds
                )
                if now - last_evaluated_at < interval:
                    return False
            news, classification = self._find_news_and_classification(candidate.news_id)
            self._evaluate(result, news, classification, now)
            return True

    def _lock_for(self, candidate_id: UUID) -> RLock:
        with self._registry_lock:
            return self._candidate_locks.setdefault(candidate_id, RLock())

    def pending_payload(self) -> list[dict[str, object]]:
        return [
            item.model_dump(mode="json")
            for item in self.candidates
            if item.state == CandidateLifecycleState.PENDING_CONFIRMATION
        ]

    def history_payload(self) -> list[dict[str, object]]:
        return [
            {
                "candidate_id": str(candidate.id),
                "news_id": str(candidate.news_id),
                "evaluations": [entry.model_dump(mode="json") for entry in candidate.evaluation_history],
            }
            for candidate in self.candidates
        ]

    def as_candidates_payload(self) -> list[dict[str, object]]:
        return [candidate.model_dump(mode="json") for candidate in self.candidates]

    def as_dry_run_payload(self) -> list[dict[str, object]]:
        with self._registry_lock:
            results = list(self.results)
        return [self._result_snapshot(result).model_dump(mode="json") for result in results]

    def result_payload(self, candidate_id: UUID) -> dict[str, object]:
        return self._result_snapshot(self.get_result(candidate_id)).model_dump(mode="json")

    def _candidate_snapshot(self, result: SignalDryRunResult) -> NewsSignalCandidate:
        return self._result_snapshot(result).candidate

    def _result_snapshot(self, result: SignalDryRunResult) -> SignalDryRunResult:
        lock = self._lock_for(result.candidate.id)
        with lock:
            return result.model_copy(deep=True)

    def _new_result(
        self,
        news: NewsItem,
        classification: NewsClassification,
        symbol: Symbol | None,
        *,
        now: datetime | None,
    ) -> SignalDryRunResult:
        now = now or datetime.now(timezone.utc)
        proposed_action = _proposed_action(classification.sentiment)
        candidate = NewsSignalCandidate(
            news_id=news.id,
            execution_environment=(
                ExecutionEnvironment.BYBIT_DEMO
                if self.settings.execution_mode == ExecutionMode.BYBIT_DEMO
                else ExecutionEnvironment.PAPER
            ),
            run_id=(
                self.settings.demo_run_id
                if self.settings.execution_mode == ExecutionMode.BYBIT_DEMO
                else None
            ),
            symbol=symbol,
            state=CandidateLifecycleState.PENDING_CONFIRMATION,
            proposed_action=proposed_action,
            final_action=NewsSignalAction.NO_TRADE,
            sentiment=classification.sentiment,
            classification_confidence=classification.confidence,
            news_importance=news.importance,
            category=classification.category,
            urgency=classification.urgency,
            market_confirmation=MarketConfirmation(),
            expected_edge_bps=0,
            proposed_stop_loss_pct=self.settings.signal_default_stop_loss_pct,
            proposed_take_profit_pct=self.settings.signal_default_take_profit_pct,
            ttl_seconds=self.settings.signal_ttl_seconds,
            reasons=[],
            created_at=now,
            expires_at=now + timedelta(seconds=self.settings.signal_ttl_seconds),
        )
        result = SignalDryRunResult(
            candidate=candidate,
            risk_preview=_preview_not_performed(),
        )
        if self.repository:
            self.repository.save_signal_result(result)
        self._evaluate(result, news, classification, now)
        return result

    def _evaluate(
        self,
        result: SignalDryRunResult,
        news: NewsItem,
        classification: NewsClassification,
        now: datetime,
        *,
        snapshot_override: MarketSnapshot | None = None,
        volume_change_override: float | None = None,
        volume_spike_override: bool | None = None,
    ) -> None:
        candidate = result.candidate
        snapshot = snapshot_override or (
            self.market_data.latest_snapshot(candidate.symbol)
            if candidate.symbol is not None
            else None
        )
        confirmation = self._confirm_market(
            classification.sentiment,
            candidate.symbol,
            snapshot,
            now,
            snapshot_is_override=snapshot_override is not None,
            volume_change_override=volume_change_override,
            volume_spike_override=volume_spike_override,
        )
        expected_edge_bps = self._expected_edge_bps(news, classification, confirmation)
        structural_reasons = self._structural_block_reasons(news, classification, candidate)

        if now >= candidate.expires_at:
            state = CandidateLifecycleState.EXPIRED
            reasons = ["market confirmation was not received before signal expiry"]
        elif structural_reasons:
            state = CandidateLifecycleState.BLOCKED
            reasons = structural_reasons
        elif self._strong_conflict(classification.sentiment, confirmation):
            state = CandidateLifecycleState.BLOCKED
            reasons = ["market moved against news beyond conflict threshold"]
        else:
            pending_reasons = list(confirmation.reasons)
            if expected_edge_bps < self.settings.signal_min_expected_edge_bps:
                pending_reasons.append("expected edge after costs is insufficient")
            ready = (
                confirmation.direction_confirmed
                and confirmation.available
                and confirmation.fresh
                and not confirmation.reasons
                and expected_edge_bps >= self.settings.signal_min_expected_edge_bps
            )
            if ready:
                state = CandidateLifecycleState.READY
                reasons = ["market confirmation and expected edge requirements passed"]
            else:
                state = CandidateLifecycleState.PENDING_CONFIRMATION
                reasons = _deduplicate(pending_reasons or ["waiting for market confirmation"])

        candidate.state = state
        candidate.final_action = (
            candidate.proposed_action
            if state == CandidateLifecycleState.READY
            else NewsSignalAction.NO_TRADE
        )
        candidate.market_confirmation = confirmation
        candidate.expected_edge_bps = expected_edge_bps
        candidate.reasons = reasons
        evaluation = SignalEvaluation(
            evaluated_at=now,
            price=snapshot.last_price if snapshot else None,
            price_change_1m_pct=confirmation.price_change_1m_pct,
            trend_direction=confirmation.trend_direction,
            volume_change_pct=confirmation.volume_change_pct,
            spread_bps=confirmation.spread_bps,
            volatility_pct=confirmation.volatility_pct,
            market_confirmed=confirmation.direction_confirmed,
            expected_edge_bps=expected_edge_bps,
            state=state,
            reasons=list(reasons),
        )
        candidate.evaluation_history.append(evaluation)
        if snapshot is not None:
            self._evaluation_snapshots[candidate.id] = snapshot
        with self._registry_lock:
            if self.last_signal_evaluation_at is None or now > self.last_signal_evaluation_at:
                self.last_signal_evaluation_at = now

        if state == CandidateLifecycleState.READY:
            result.risk_preview = self._perform_risk_preview(candidate, snapshot)
            with self._registry_lock:
                if result.risk_preview.approved:
                    self.risk_preview_approved_count += 1
                else:
                    self.risk_preview_blocked_count += 1
            if self.settings.execution_mode == ExecutionMode.PAPER and self.settings.auto_paper_execution:
                self._execute_paper_result(result, classification, snapshot)
            elif (
                self.settings.execution_mode == ExecutionMode.BYBIT_DEMO
                and self.settings.bybit_demo_trading_enabled
            ):
                self._execute_demo_result(result, classification, snapshot)
        else:
            result.risk_preview = _preview_not_performed()
        if self.repository:
            self.repository.save_signal_result(result)

    def _structural_block_reasons(
        self,
        news: NewsItem,
        classification: NewsClassification,
        candidate: NewsSignalCandidate,
    ) -> list[str]:
        reasons: list[str] = []
        if self.settings.execution_mode == ExecutionMode.PAPER:
            if self.settings.bot_mode != BotMode.PAPER:
                reasons.append("paper execution requires PAPER mode")
        elif self.settings.bot_mode != BotMode.BYBIT_DEMO:
            reasons.append("Demo execution requires BYBIT_DEMO mode")
        if classification.sentiment == Sentiment.NEUTRAL:
            reasons.append("neutral classification")
        if classification.confidence < self.settings.signal_min_classification_confidence:
            reasons.append("classification confidence below signal threshold")
        if news.importance < self.settings.signal_min_news_importance:
            reasons.append("news importance below signal threshold")
        if candidate.symbol is None:
            reasons.append("news asset cannot be mapped to a supported symbol")
        if (
            candidate.symbol is not None
            and self.settings.execution_mode == ExecutionMode.PAPER
        ):
            reasons.extend(self.paper_trading.entry_block_reasons(candidate.symbol))
        return reasons

    def _confirm_market(
        self,
        sentiment: Sentiment,
        symbol: Symbol | None,
        snapshot: MarketSnapshot | None,
        now: datetime,
        *,
        snapshot_is_override: bool = False,
        volume_change_override: float | None = None,
        volume_spike_override: bool | None = None,
    ) -> MarketConfirmation:
        if (
            symbol is None
            or snapshot is None
            or (not snapshot_is_override and self.market_data.status != "OK")
        ):
            return MarketConfirmation(reasons=["market data is unavailable"])

        fresh = now - snapshot.timestamp <= timedelta(
            seconds=self.settings.signal_confirmation_window_seconds
        )
        reasons: list[str] = []
        if not fresh:
            reasons.append("market data is stale")
        if snapshot.spread_bps > self.settings.max_spread_bps:
            reasons.append("spread is too wide")
        if not 0 <= snapshot.volatility_pct <= 8.0:
            reasons.append("volatility is outside allowed range")

        direction_confirmed = False
        if sentiment == Sentiment.BULLISH:
            direction_confirmed = (
                snapshot.price_change_1m_pct > 0
                and snapshot.trend_score > 0
                and snapshot.simple_trend == SimpleTrend.BULLISH
            )
        elif sentiment == Sentiment.BEARISH:
            direction_confirmed = (
                snapshot.price_change_1m_pct < 0
                and snapshot.trend_score < 0
                and snapshot.simple_trend == SimpleTrend.BEARISH
            )
        if sentiment != Sentiment.NEUTRAL and not direction_confirmed:
            reasons.append("market direction is not confirmed")

        volume_change_pct = (
            volume_change_override
            if snapshot_is_override
            else self._volume_change_pct(symbol)
        )
        volume_spike = (
            volume_spike_override
            if snapshot_is_override
            else (volume_change_pct >= 20 if volume_change_pct is not None else None)
        )
        return MarketConfirmation(
            available=True,
            fresh=fresh,
            direction_confirmed=direction_confirmed,
            price_change_1m_pct=snapshot.price_change_1m_pct,
            trend_direction=snapshot.simple_trend.value,
            trend_score=snapshot.trend_score,
            spread_bps=snapshot.spread_bps,
            volatility_pct=snapshot.volatility_pct,
            volume_24h=snapshot.volume_24h,
            volume_change_pct=volume_change_pct,
            volume_spike=volume_spike,
            reasons=reasons,
        )

    def _strong_conflict(
        self, sentiment: Sentiment, confirmation: MarketConfirmation
    ) -> bool:
        if not confirmation.available or not confirmation.fresh:
            return False
        change = confirmation.price_change_1m_pct
        if change is None:
            return False
        threshold = self.settings.signal_conflict_threshold_pct
        return (
            sentiment == Sentiment.BULLISH and change <= -threshold
        ) or (
            sentiment == Sentiment.BEARISH and change >= threshold
        )

    def _volume_change_pct(self, symbol: Symbol) -> float | None:
        history = self.market_data.history.get(symbol, [])
        if len(history) < 2:
            return None
        previous = history[-2].volume_24h
        current = history[-1].volume_24h
        if previous is None or current is None or previous <= 0:
            return None
        return (current - previous) / previous * 100

    def _expected_edge_bps(
        self,
        news: NewsItem,
        classification: NewsClassification,
        confirmation: MarketConfirmation,
    ) -> float:
        if not confirmation.available:
            return 0.0
        price_component = abs(confirmation.price_change_1m_pct or 0) * 100
        trend_component = abs(confirmation.trend_score or 0) * 20
        volume_component = 5.0 if confirmation.volume_spike else 0.0
        urgency_component = 3.0 if classification.urgency.lower() == "high" else 1.0
        category_component = (
            2.0
            if classification.category.lower()
            in {"etf", "security", "regulation", "macro", "listing", "exchange"}
            else 0.0
        )
        gross_edge = (
            price_component * classification.confidence
            + trend_component
            + news.importance * 5
            + volume_component
            + urgency_component
            + category_component
        )
        costs = (
            self.settings.default_paper_fees_bps + self.settings.default_slippage_bps
        ) * 2
        target_cap = self.settings.signal_default_take_profit_pct * 100
        return round(max(0.0, min(gross_edge - costs, target_cap)), 4)

    def _perform_risk_preview(
        self, candidate: NewsSignalCandidate, snapshot: MarketSnapshot | None
    ) -> SignalRiskPreview:
        if snapshot is None:
            return _preview_not_performed()
        signal = TradeSignal(
            action=SignalAction.TRADE,
            symbol=snapshot.symbol,
            side=Side.BUY if candidate.final_action == NewsSignalAction.BUY else Side.SELL,
            confidence=candidate.classification_confidence,
            expected_edge_bps=candidate.expected_edge_bps,
            stop_loss_pct=candidate.proposed_stop_loss_pct,
            take_profit_pct=candidate.proposed_take_profit_pct,
            reasons=list(candidate.reasons),
        )
        demo_mode = self.settings.execution_mode == ExecutionMode.BYBIT_DEMO
        risk_equity = risk_capital_for_execution(
            self.settings, self.paper_trading.equity
        )
        active_demo_states = {
            CandidateLifecycleState.DEMO_SUBMITTING,
            CandidateLifecycleState.DEMO_ACCEPTED,
            CandidateLifecycleState.DEMO_PARTIALLY_FILLED,
            CandidateLifecycleState.DEMO_FILLED,
            CandidateLifecycleState.DEMO_PROTECTION_PENDING,
            CandidateLifecycleState.DEMO_POSITION_OPEN,
            CandidateLifecycleState.DEMO_CLOSING,
            CandidateLifecycleState.DEMO_RECONCILIATION_REQUIRED,
        }
        context = RiskContext(
            equity=risk_equity,
            available_balance=risk_equity,
            requested_risk_pct=self.settings.max_risk_per_trade_pct,
            leverage=self.settings.demo_leverage if demo_mode else self.settings.max_leverage,
            open_positions=(
                sum(result.candidate.state in active_demo_states for result in self.results)
                if demo_mode else len(self.paper_trading.open_positions)
            ),
            daily_pnl_pct=self.settings.paper_daily_pnl_pct,
            weekly_pnl_pct=self.settings.paper_weekly_pnl_pct,
            consecutive_losses=self.settings.paper_consecutive_losses,
            api_stable=self.market_data.status == "OK",
        )
        decision = RiskManager(_risk_rules(self.settings)).assess(signal, snapshot, context)
        risk_decision_id = (
            self.repository.save_risk_decision(str(candidate.id), decision)
            if self.repository else None
        )
        return SignalRiskPreview(
            preview_performed=True,
            approved=decision.approved,
            capped_size=decision.capped_size,
            position_notional=decision.position_notional,
            max_allowed_notional=decision.max_allowed_notional,
            rejection_reasons=decision.reasons,
            risk_decision_id=risk_decision_id,
            estimated_fees=decision.estimated_fees,
            estimated_slippage=decision.estimated_slippage,
        )

    def execute_ready_candidate(
        self, candidate_id: UUID, *, force: bool = False
    ) -> SignalDryRunResult:
        result = self.get_result(candidate_id)
        news, classification = self._find_news_and_classification(
            result.candidate.news_id
        )
        del news
        snapshot = self._evaluation_snapshots.get(candidate_id) or (
            self.market_data.latest_snapshot(result.candidate.symbol)
            if result.candidate.symbol else None
        )
        if self.settings.execution_mode == ExecutionMode.BYBIT_DEMO:
            if not self.settings.bybit_demo_trading_enabled:
                return result
            self._execute_demo_result(result, classification, snapshot)
        else:
            if not force and not self.settings.auto_paper_execution:
                return result
            self._execute_paper_result(result, classification, snapshot)
        return result

    def execute_ready_candidates(self) -> None:
        enabled = (
            self.settings.auto_paper_execution
            if self.settings.execution_mode == ExecutionMode.PAPER
            else self.settings.bybit_demo_trading_enabled
        )
        if not enabled:
            return
        for result in list(self.results):
            if result.candidate.state == CandidateLifecycleState.READY:
                self.execute_ready_candidate(result.candidate.id)

    def execute_demo_canary(
        self,
        symbol: Symbol,
        notional_usdt: float,
        snapshot: MarketSnapshot,
    ) -> SignalDryRunResult:
        """Create one deterministic, durably risk-approved candidate for a Demo canary."""
        if (
            self.settings.execution_mode != ExecutionMode.BYBIT_DEMO
            or not self.settings.demo_canary_enabled
            or self.demo_execution is None
            or self.repository is None
            or not self.repository.available
        ):
            raise ValueError("Demo canary execution is unavailable")
        run_id = str(getattr(self.demo_execution, "run_id", ""))
        if not run_id:
            raise ValueError("Demo canary run ID is unavailable")
        self.demo_execution.validate_canary_notional(
            symbol,
            Decimal(str(notional_usdt)),
            Decimal(str(snapshot.ask_price)),
        )
        candidate_id = uuid5(NAMESPACE_URL, f"bybot-demo-canary:{run_id}:{symbol.value}")
        existing = self.repository.get_demo_execution(str(candidate_id))
        if existing is not None:
            result = next(
                (item for item in self.results if item.candidate.id == candidate_id),
                None,
            )
            if result is None:
                result = next(
                    (
                        item for item in self.repository.load_signal_results()
                        if item.candidate.id == candidate_id
                    ),
                    None,
                )
            if result is None:
                raise ValueError("durable Demo canary candidate is unavailable")
            result.demo_execution = existing.model_dump(mode="json")
            result.execution_attempted = True
            return result

        now = datetime.now(timezone.utc)
        news = NewsItem(
            id=uuid5(NAMESPACE_URL, f"bybot-demo-canary-news:{run_id}:{symbol.value}"),
            title=f"Controlled ByBot Demo canary {run_id} {symbol.value}",
            summary="Operator-authorized Demo-only execution canary; not a market signal.",
            source="bybot-demo-canary",
            published_at=now,
            received_at=now,
            asset_hint=Asset.BTC if symbol == Symbol.BTCUSDT else Asset.ETH,
            importance=1.0,
        )
        classification = NewsClassification(
            news_id=news.id,
            asset=news.asset_hint,
            sentiment=Sentiment.BULLISH,
            confidence=1.0,
            category="other",
            urgency="normal",
            reason="explicit operator-authorized Demo canary",
            model_name="deterministic-demo-canary",
            provider_name="deterministic",
            classifier_version="demo-canary-v1",
            classification_status=ClassificationStatus.SUCCESS,
            trade_eligible=True,
            classified_at=now,
        )
        candidate = NewsSignalCandidate(
            id=candidate_id,
            news_id=news.id,
            execution_environment=ExecutionEnvironment.BYBIT_DEMO,
            run_id=run_id,
            symbol=symbol,
            state=CandidateLifecycleState.READY,
            proposed_action=NewsSignalAction.BUY,
            final_action=NewsSignalAction.BUY,
            sentiment=Sentiment.BULLISH,
            classification_confidence=1.0,
            news_importance=1.0,
            category="other",
            urgency="normal",
            market_confirmation=MarketConfirmation(
                available=True,
                fresh=True,
                direction_confirmed=True,
                price_change_1m_pct=snapshot.price_change_1m_pct,
                trend_direction=snapshot.simple_trend.value,
                trend_score=snapshot.trend_score,
                spread_bps=snapshot.spread_bps,
                volatility_pct=snapshot.volatility_pct,
                volume_24h=snapshot.volume_24h,
                reasons=["controlled Demo canary market snapshot"],
            ),
            expected_edge_bps=max(25.0, self.settings.signal_min_expected_edge_bps),
            proposed_stop_loss_pct=self.settings.signal_default_stop_loss_pct,
            proposed_take_profit_pct=self.settings.signal_default_take_profit_pct,
            ttl_seconds=self.settings.signal_ttl_seconds,
            reasons=["controlled operator-authorized Demo canary"],
            created_at=now,
            expires_at=now + timedelta(seconds=self.settings.signal_ttl_seconds),
        )
        result = SignalDryRunResult(candidate=candidate, risk_preview=SignalRiskPreview())
        if not self.repository.save_news(news):
            # A deterministic retry may encounter the already persisted news row.
            if not any(item.id == news.id for item in self.repository.load_news()[0]):
                raise ValueError("Demo canary news could not be persisted")
        self.repository.save_classification(
            news,
            classification,
            "demo-canary-v1",
            now + timedelta(days=1),
        )
        self.repository.save_signal_result(result)

        rules = replace(
            _risk_rules(self.settings),
            max_position_notional_usdt=min(
                float(notional_usdt), self.settings.max_position_notional_usdt
            ),
        )
        signal = TradeSignal(
            action=SignalAction.TRADE,
            symbol=symbol,
            side=Side.BUY,
            confidence=1.0,
            expected_edge_bps=candidate.expected_edge_bps,
            stop_loss_pct=candidate.proposed_stop_loss_pct,
            take_profit_pct=candidate.proposed_take_profit_pct,
            reasons=list(candidate.reasons),
        )
        capital = float(self.settings.demo_risk_capital_usdt)
        decision: RiskDecision = RiskManager(rules).assess(
            signal,
            snapshot,
            RiskContext(
                equity=capital,
                available_balance=capital,
                requested_risk_pct=self.settings.max_risk_per_trade_pct,
                leverage=self.settings.demo_leverage,
                open_positions=0,
                daily_pnl_pct=self.settings.paper_daily_pnl_pct,
                weekly_pnl_pct=self.settings.paper_weekly_pnl_pct,
                consecutive_losses=self.settings.paper_consecutive_losses,
                api_stable=snapshot.api_stable,
            ),
        )
        risk_decision_id = self.repository.save_risk_decision(str(candidate.id), decision)
        result.risk_preview = SignalRiskPreview(
            preview_performed=True,
            approved=decision.approved,
            capped_size=decision.capped_size,
            position_notional=decision.position_notional,
            max_allowed_notional=decision.max_allowed_notional,
            rejection_reasons=list(decision.reasons),
            risk_decision_id=risk_decision_id,
            estimated_fees=decision.estimated_fees,
            estimated_slippage=decision.estimated_slippage,
        )
        self.repository.save_signal_result(result)
        if not decision.approved or risk_decision_id is None:
            result.execution_block_reason = "Demo canary risk decision was not approved"
            return result
        record = self.demo_execution.submit_candidate(
            candidate, result.risk_preview, classification, snapshot
        )
        if record is None:
            result.execution_block_reason = (
                getattr(self.demo_execution, "last_error", None)
                or "Demo canary submission was blocked"
            )
            return result
        candidate.state = CandidateLifecycleState(record.state.value)
        result.execution_attempted = True
        result.demo_execution = record.model_dump(mode="json")
        with self._registry_lock:
            self.results.append(result)
            self._candidate_locks[candidate.id] = RLock()
        self.news_service.items.append(news)
        self.news_service.filtered_items.append(news)
        self.news_service.classifications.append(classification)
        self.repository.save_signal_result(result)
        return result

    def _execute_demo_result(
        self,
        result: SignalDryRunResult,
        classification: NewsClassification,
        snapshot: MarketSnapshot | None,
    ) -> None:
        """Delegate only fully approved candidates to the guarded Demo adapter."""
        if self.demo_execution is None or snapshot is None:
            result.execution_block_reason = "Demo execution service is unavailable"
            return
        candidate = result.candidate
        now = datetime.now(timezone.utc)
        if (
            candidate.execution_environment != ExecutionEnvironment.BYBIT_DEMO
            or candidate.state != CandidateLifecycleState.READY
            or candidate.final_action not in {NewsSignalAction.BUY, NewsSignalAction.SELL}
            or not result.risk_preview.preview_performed
            or not result.risk_preview.approved
            or now >= candidate.expires_at
            or not classification.trade_eligible
            or not candidate.market_confirmation.fresh
            or now - snapshot.timestamp > timedelta(
                seconds=self.settings.signal_confirmation_window_seconds
            )
        ):
            result.execution_block_reason = "candidate failed Demo execution preflight"
            return
        record = self.demo_execution.submit_candidate(
            candidate, result.risk_preview, classification, snapshot
        )
        if record is None:
            result.execution_block_reason = (
                getattr(self.demo_execution, "last_error", None)
                or "Demo execution was blocked"
            )
            return
        candidate.state = CandidateLifecycleState(record.state.value)
        result.execution_attempted = True
        result.demo_execution = record.model_dump(mode="json")
        if self.repository:
            self.repository.save_signal_result(result)

    def _execute_paper_result(
        self,
        result: SignalDryRunResult,
        classification: NewsClassification,
        snapshot: MarketSnapshot | None,
    ) -> None:
        candidate = result.candidate
        now = datetime.now(timezone.utc)
        reasons: list[str] = []
        if candidate.execution_environment != ExecutionEnvironment.PAPER:
            reasons.append("candidate execution environment is not PAPER")
        if candidate.state != CandidateLifecycleState.READY:
            reasons.append("candidate is not READY")
        if candidate.final_action not in {NewsSignalAction.BUY, NewsSignalAction.SELL}:
            reasons.append("candidate has no executable paper direction")
        if not result.risk_preview.preview_performed or not result.risk_preview.approved:
            reasons.append("risk preview is not approved")
        if now >= candidate.expires_at:
            reasons.append("candidate is expired")
        if not classification.trade_eligible:
            reasons.append("classification is not trade eligible")
        snapshot_fresh = (
            snapshot is not None
            and now - snapshot.timestamp <= timedelta(
                seconds=self.settings.signal_confirmation_window_seconds
            )
        )
        if not snapshot_fresh or not candidate.market_confirmation.fresh:
            reasons.append("market data is stale or unavailable")
        if candidate.symbol is not None:
            reasons.extend(self.paper_trading.entry_block_reasons(candidate.symbol))
        if str(candidate.id) in self.paper_trading.executed_candidate_ids:
            self.paper_trading.paper_execution_duplicates_blocked += 1
            self.paper_trading.last_execution_attempted = False
            self.paper_trading.last_position_opened = False
            self.paper_trading.last_execution_duplicate = True
            existing = next(
                (
                    item for item in self.paper_trading.positions
                    if item.candidate_id == candidate.id
                ),
                None,
            )
            self.paper_trading.last_execution_details = (
                existing.model_dump(mode="json") if existing else None
            )
            self.paper_trading.last_existing_execution_state = (
                CandidateLifecycleState.PAPER_CLOSED.value
                if existing and existing.status.value == "CLOSED"
                else CandidateLifecycleState.PAPER_OPENED.value
                if existing else None
            )
            result.execution_attempted = False
            result.paper_position_opened = False
            result.execution_block_reason = None
            return
        if reasons:
            candidate.state = CandidateLifecycleState.EXECUTION_BLOCKED
            candidate.final_action = NewsSignalAction.NO_TRADE
            candidate.reasons = reasons
            result.execution_attempted = False
            result.paper_position_opened = False
            result.execution_block_reason = "; ".join(reasons)
            result.execution_error_code = None
            result.execution_retryable = False
            if not result.risk_preview.approved:
                self.paper_trading.paper_execution_risk_blocked += 1
            if self.repository:
                self.repository.save_signal_result(result)
            return
        original_state = candidate.state
        original_action = candidate.final_action
        candidate.state = CandidateLifecycleState.EXECUTING_PAPER
        position = self.paper_trading.open_from_candidate(
            candidate, result.risk_preview, snapshot,
            taker_fee_bps=self.settings.paper_taker_fee_bps,
            slippage_bps=self.settings.paper_slippage_bps,
        )
        result.execution_attempted = self.paper_trading.last_execution_attempted
        result.paper_position_opened = self.paper_trading.last_position_opened
        result.execution_block_reason = None
        result.execution_error_code = self.paper_trading.last_execution_error_code
        result.execution_retryable = self.paper_trading.last_execution_retryable
        if self.paper_trading.last_execution_error_code:
            candidate.state = original_state
            candidate.final_action = original_action
            candidate.reasons = ["paper execution persistence temporarily unavailable"]
            result.execution_block_reason = None
            if self.repository:
                self.repository.save_signal_result(result)
            return
        if self.paper_trading.last_execution_duplicate:
            candidate.final_action = original_action
            if position is not None:
                candidate.state = (
                    CandidateLifecycleState.PAPER_CLOSED
                    if position.status.value == "CLOSED"
                    else CandidateLifecycleState.PAPER_OPENED
                )
            else:
                existing_state = self.paper_trading.last_existing_execution_state
                candidate.state = (
                    CandidateLifecycleState(existing_state)
                    if existing_state in {
                        CandidateLifecycleState.PAPER_OPENED.value,
                        CandidateLifecycleState.PAPER_CLOSED.value,
                    }
                    else original_state
                )
            if self.repository:
                self.repository.save_signal_result(result)
            return
        candidate.state = (
            CandidateLifecycleState.PAPER_OPENED
            if position else CandidateLifecycleState.EXECUTION_BLOCKED
        )
        if position is None:
            candidate.final_action = NewsSignalAction.NO_TRADE
            candidate.reasons = [self.paper_trading.last_error or "paper execution blocked"]
            result.execution_block_reason = candidate.reasons[0]
        if self.repository:
            self.repository.save_signal_result(result)

    def sync_paper_states(self) -> None:
        for result in self.results:
            candidate = result.candidate
            position = next(
                (
                    item for item in self.paper_trading.positions
                    if item.candidate_id == candidate.id
                ),
                None,
            )
            if position and position.status.value == "CLOSED" and candidate.state != CandidateLifecycleState.PAPER_CLOSED:
                candidate.state = CandidateLifecycleState.PAPER_CLOSED
                if self.repository:
                    self.repository.save_signal_result(result)

    def sync_demo_states(self) -> None:
        if self.demo_execution is None or self.repository is None:
            return
        records = {
            record.candidate_id: record
            for record in self.repository.load_demo_executions()
        }
        for result in self.results:
            record = records.get(result.candidate.id)
            if record is None:
                continue
            state = CandidateLifecycleState(record.state.value)
            if result.candidate.state != state:
                result.candidate.state = state
                result.demo_execution = record.model_dump(mode="json")
                self.repository.save_signal_result(result)

    def _find_news_and_classification(
        self, news_id: UUID
    ) -> tuple[NewsItem, NewsClassification]:
        news = next((item for item in self.news_service.items if item.id == news_id), None)
        classification = next(
            (item for item in self.news_service.classifications if item.news_id == news_id), None
        )
        if news is None or classification is None:
            raise ValueError("news item or classification not found")
        return news, classification


def _preview_not_performed() -> SignalRiskPreview:
    return SignalRiskPreview(
        preview_performed=False,
        preview_reason="candidate is not tradeable yet",
    )


def risk_capital_for_execution(settings: Settings, paper_equity: float) -> float:
    """Demo sizing is deliberately detached from both paper and wallet equity."""
    if settings.execution_mode == ExecutionMode.BYBIT_DEMO:
        return float(settings.demo_risk_capital_usdt)
    return paper_equity


def _proposed_action(sentiment: Sentiment) -> NewsSignalAction:
    if sentiment == Sentiment.BULLISH:
        return NewsSignalAction.BUY
    if sentiment == Sentiment.BEARISH:
        return NewsSignalAction.SELL
    return NewsSignalAction.NO_TRADE


def _symbols_for_asset(asset: Asset) -> tuple[Symbol, ...]:
    if asset == Asset.BTC:
        return (Symbol.BTCUSDT,)
    if asset == Asset.ETH:
        return (Symbol.ETHUSDT,)
    if asset == Asset.MARKET:
        return (Symbol.BTCUSDT, Symbol.ETHUSDT)
    return ()


def _deduplicate(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _risk_rules(settings: Settings) -> RiskRules:
    return RiskRules(
        max_open_positions=settings.paper_max_total_open_positions,
        max_risk_per_trade_pct=settings.max_risk_per_trade_pct,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_weekly_loss_pct=settings.max_weekly_loss_pct,
        max_leverage=settings.max_leverage,
        max_spread_bps=settings.max_spread_bps,
        min_confidence=settings.signal_min_classification_confidence,
        min_expected_edge_bps=settings.signal_min_expected_edge_bps,
        max_position_notional_usdt=settings.max_position_notional_usdt,
        max_position_notional_pct_of_equity=settings.max_position_notional_pct_of_equity,
        min_position_notional_usdt=settings.min_position_notional_usdt,
        default_paper_fees_bps=settings.paper_taker_fee_bps,
        default_slippage_bps=settings.paper_slippage_bps,
        min_net_edge_bps=settings.min_net_edge_bps,
    )
