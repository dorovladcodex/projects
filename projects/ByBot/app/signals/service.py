from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock
from uuid import UUID

from app.bybit.market_data import MarketDataService
from app.bybit.private import BybitAccountService
from app.config import Settings
from app.models import (
    Asset,
    CandidateLifecycleState,
    ClassificationStatus,
    MarketConfirmation,
    MarketSnapshot,
    NewsClassification,
    NewsItem,
    NewsSignalAction,
    NewsSignalCandidate,
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


class SignalCandidateService:
    """Maintain dry-run candidates and re-evaluate them without execution."""

    def __init__(
        self,
        settings: Settings,
        news_service: NewsService,
        market_data: MarketDataService,
        account_service: BybitAccountService,
        paper_trading: PaperTradingService,
    ) -> None:
        self.settings = settings
        self.news_service = news_service
        self.market_data = market_data
        self.account_service = account_service
        self.paper_trading = paper_trading
        self.processed_news_ids: set[UUID] = set()
        self.results: list[SignalDryRunResult] = []
        self.risk_preview_approved_count = 0
        self.risk_preview_blocked_count = 0
        self.last_signal_evaluation_at: datetime | None = None
        self._registry_lock = RLock()
        self._candidate_locks: dict[UUID, RLock] = {}

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
        else:
            result.risk_preview = _preview_not_performed()

    def _structural_block_reasons(
        self,
        news: NewsItem,
        classification: NewsClassification,
        candidate: NewsSignalCandidate,
    ) -> list[str]:
        reasons: list[str] = []
        if classification.sentiment == Sentiment.NEUTRAL:
            reasons.append("neutral classification")
        if classification.confidence < self.settings.signal_min_classification_confidence:
            reasons.append("classification confidence below signal threshold")
        if news.importance < self.settings.signal_min_news_importance:
            reasons.append("news importance below signal threshold")
        if candidate.symbol is None:
            reasons.append("news asset cannot be mapped to a supported symbol")
        if self.paper_trading.open_position is not None:
            reasons.append("an open paper position already exists")
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
        account = self.account_service.status
        context = RiskContext(
            equity=account.equity or self.settings.paper_starting_equity,
            available_balance=account.available_balance,
            requested_risk_pct=self.settings.max_risk_per_trade_pct,
            leverage=self.settings.max_leverage,
            open_positions=1 if self.paper_trading.open_position else 0,
            daily_pnl_pct=self.settings.paper_daily_pnl_pct,
            weekly_pnl_pct=self.settings.paper_weekly_pnl_pct,
            consecutive_losses=self.settings.paper_consecutive_losses,
            api_stable=self.market_data.status == "OK",
        )
        decision = RiskManager(_risk_rules(self.settings)).assess(signal, snapshot, context)
        return SignalRiskPreview(
            preview_performed=True,
            approved=decision.approved,
            capped_size=decision.capped_size,
            position_notional=decision.position_notional,
            max_allowed_notional=decision.max_allowed_notional,
            rejection_reasons=decision.reasons,
        )

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
        default_paper_fees_bps=settings.default_paper_fees_bps,
        default_slippage_bps=settings.default_slippage_bps,
        min_net_edge_bps=settings.min_net_edge_bps,
    )
