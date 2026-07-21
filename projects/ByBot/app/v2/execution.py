from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.bybit.demo import DemoExecutionService, DemoSafetyError, require_demo_execution
from app.config import ExecutionMode, Settings
from app.models import (
    Asset, CandidateLifecycleState, ClassificationStatus, ExecutionEnvironment,
    MarketConfirmation, MarketSnapshot, NewsClassification, NewsItem,
    NewsSignalAction, NewsSignalCandidate, RiskDecision, SignalDryRunResult,
    SignalRiskPreview, SimpleTrend,
)
from app.v2.models import ReservationState, StrategySide, V2SignalCandidate
from app.v2.portfolio import (
    PortfolioRiskService, normalize_leverage, normalize_order_quantity,
)
from app.v2.universe import SymbolUniverseService


@dataclass(frozen=True)
class ExpectedDemoPolicyRejection:
    code: str
    risk_control: str


EXPECTED_DEMO_POLICY_REJECTIONS: dict[str, ExpectedDemoPolicyRejection] = {
    "symbol cooldown is active": ExpectedDemoPolicyRejection(
        "SYMBOL_COOLDOWN_ACTIVE", "symbol_cooldown"
    ),
    "global entry cooldown is active": ExpectedDemoPolicyRejection(
        "GLOBAL_ENTRY_COOLDOWN_ACTIVE", "global_entry_cooldown"
    ),
    "maximum total Demo positions reached": ExpectedDemoPolicyRejection(
        "MAXIMUM_POSITIONS_REACHED", "maximum_total_positions"
    ),
    "conflicting remote Demo position exists": ExpectedDemoPolicyRejection(
        "DUPLICATE_POSITION", "per_symbol_position_limit"
    ),
    "conflicting active Demo order exists": ExpectedDemoPolicyRejection(
        "CONFLICTING_ACTIVE_ORDER", "per_symbol_order_limit"
    ),
    "maximum daily Demo net loss reached": ExpectedDemoPolicyRejection(
        "DAILY_LOSS_LIMIT_REACHED", "daily_loss_limit"
    ),
    "maximum weekly Demo net loss reached": ExpectedDemoPolicyRejection(
        "WEEKLY_LOSS_LIMIT_REACHED", "weekly_loss_limit"
    ),
    "maximum Demo account drawdown reached": ExpectedDemoPolicyRejection(
        "DRAWDOWN_LIMIT_REACHED", "drawdown_limit"
    ),
}


def classify_expected_demo_policy_rejection(
    exc: DemoSafetyError,
) -> ExpectedDemoPolicyRejection | None:
    parts = [part.strip() for part in str(exc).split(";") if part.strip()]
    matches = [EXPECTED_DEMO_POLICY_REJECTIONS.get(part) for part in parts]
    if not parts or any(item is None for item in matches):
        return None
    resolved = [item for item in matches if item is not None]
    if len(resolved) == 1:
        return resolved[0]
    return ExpectedDemoPolicyRejection(
        code="MULTIPLE_RISK_LIMITS_REACHED",
        risk_control="+".join(item.risk_control for item in resolved),
    )


class V2ExecutionCoordinator:
    """Admitted V2 signal -> existing durable Demo state machine only."""

    def __init__(
        self, settings: Settings, repository: Any,
        universe: SymbolUniverseService, portfolio: PortfolioRiskService,
        demo_execution: DemoExecutionService,
        *, run_id: str,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.universe = universe
        self.portfolio = portfolio
        self.demo_execution = demo_execution
        self.run_id = run_id
        self._locks = {symbol: RLock() for symbol in universe.statuses}
        self.last_error: str | None = None

    def safety_preflight(self, *, require_auto_execution: bool = True) -> list[str]:
        reasons: list[str] = []
        if not self.settings.v2_enabled:
            reasons.append("V2 is disabled")
        if require_auto_execution and not self.settings.v2_auto_demo_execution:
            reasons.append("V2 automatic Demo execution is disabled")
        if self.settings.execution_mode != ExecutionMode.BYBIT_DEMO:
            reasons.append("execution mode is not BYBIT_DEMO")
        try:
            require_demo_execution(self.settings)
        except DemoSafetyError as exc:
            reasons.append(str(exc))
        if self.settings.bybit_live_trading_enabled or self.settings.bybit_enable_trading:
            reasons.append("live/generic Bybit execution flag is enabled")
        if self.settings.bybit_env.value != "demo":
            reasons.append("authenticated environment is not Demo")
        if self.demo_execution.kill_switch_active or self.portfolio.kill_switch_active:
            reasons.append("kill switch is active")
        unresolved = [
            item for item in self.repository.load_demo_executions()
            if item.state.value not in {
                "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE", "DEMO_NOT_SUBMITTED",
                "DEMO_ORDER_CANCELLED", "DEMO_CLOSED_AFTER_INTERRUPTION",
                "DEMO_CLOSED_EXTERNALLY", "DEMO_FAILED_FLAT_VERIFIED",
            }
        ]
        # An execution owned by this process may be open; unresolved unsafe startup
        # state is blocked until the existing reconciler has verified it.
        if any(item.last_error and not item.protection_confirmed for item in unresolved):
            reasons.append("unresolved unsafe Demo execution exists")
        if not self.repository.available:
            reasons.append("persistence is unavailable")
        if not self.demo_execution.account_verified:
            reasons.append("Demo account is not verified")
        return list(dict.fromkeys(reasons))

    def execute(self, candidate: V2SignalCandidate) -> dict[str, Any]:
        candidate.execution_task_received_at = datetime.now(timezone.utc)
        symbol_lock = self._locks.setdefault(candidate.symbol, RLock())
        with symbol_lock, self.portfolio.symbol_lock(candidate.symbol):
            return self._execute_locked(candidate)

    def _execute_locked(self, candidate: V2SignalCandidate) -> dict[str, Any]:
        if not candidate.admitted or candidate.state != "READY":
            return self._blocked(candidate, "candidate is not admitted and READY")
        if datetime.now(timezone.utc) >= candidate.expires_at:
            return self._blocked(candidate, "candidate expired")
        preflight = self.safety_preflight()
        if preflight:
            return self._blocked(candidate, "; ".join(preflight))
        universe_status = self.universe.get(candidate.symbol)
        if universe_status is None or not universe_status.accepted or universe_status.instrument is None:
            return self._blocked(candidate, "symbol is not in the validated universe")
        if not candidate.feature_snapshot.fresh:
            return self._blocked(candidate, "mandatory market data is stale")
        target_notional = self.settings.v2_target_notional_for_symbol(candidate.symbol.value)
        active_notional = sum(
            row.notional_usdt for row in self.portfolio.reservations
            if row.state in self.portfolio.ACTIVE
        )
        max_for_position = min(
            target_notional,
            self.settings.max_total_notional_usdt - active_notional,
        )
        entry_price = (
            candidate.feature_snapshot.ask_price
            if candidate.side == StrategySide.LONG
            else candidate.feature_snapshot.bid_price
        )
        try:
            quantity = normalize_order_quantity(
                target_notional, entry_price, universe_status.instrument,
                max_for_position,
            )
        except ValueError as exc:
            return self._blocked(candidate, str(exc))
        notional = quantity * entry_price
        risk_usdt = notional * candidate.stop_loss_pct / Decimal("100")
        if risk_usdt > self.settings.risk_capital_usdt * self.settings.max_portfolio_risk_pct / Decimal("100"):
            return self._blocked(candidate, "position risk exceeds portfolio risk capital")
        candidate.reservation_requested_at = datetime.now(timezone.utc)
        self.repository.save_v2_signal_candidate(candidate)
        reservation = self.portfolio.reserve(
            run_id=self.run_id, candidate_id=candidate.id, symbol=candidate.symbol,
            strategy_name=candidate.strategy_name, notional=notional,
            risk_usdt=risk_usdt,
        )
        if reservation is None:
            return self._blocked(candidate, "durable portfolio reservation was rejected")
        candidate.reservation_created_at = reservation.created_at
        candidate.reservation_id = reservation.id
        candidate.risk_evaluation_started_at = datetime.now(timezone.utc)
        self.repository.save_v2_signal_candidate(candidate)
        compatibility = self._persist_compatibility_candidate(candidate, quantity, notional)
        if compatibility is None:
            self.portfolio.release(reservation.id, activate_cooldown=False)
            return self._persistence_blocked(candidate)
        result, classification = compatibility
        candidate.risk_approved_at = datetime.now(timezone.utc)
        candidate.execution_dispatched_at = datetime.now(timezone.utc)
        self.repository.save_v2_signal_candidate(candidate)
        reservation.state = ReservationState.EXECUTING
        self.repository.update_v2_portfolio_reservation(reservation)
        leverage = normalize_leverage(
            self.settings.v2_leverage_for_symbol(candidate.symbol.value),
            universe_status.instrument,
        )
        try:
            record = self.demo_execution.submit_candidate(
                result.candidate, result.risk_preview, classification,
                _market_snapshot(candidate), desired_leverage=leverage,
                strategy_name=candidate.strategy_name.value,
                strategy_version=candidate.strategy_version,
                trailing_stop_pct=candidate.trailing_stop_pct,
                break_even_at_r=candidate.break_even_at_r,
                maximum_holding_seconds=candidate.maximum_holding_seconds,
                latency_timeline={
                    "candidate_persisted_at": candidate.candidate_persisted_at,
                    "reservation_requested_at": candidate.reservation_requested_at,
                    "reservation_created_at": candidate.reservation_created_at,
                    "risk_evaluation_started_at": candidate.risk_evaluation_started_at,
                    "risk_approved_at": candidate.risk_approved_at,
                    "execution_dispatched_at": candidate.execution_dispatched_at,
                    "execution_task_received_at": candidate.execution_task_received_at,
                },
                instrument_rules=universe_status.instrument,
            )
        except DemoSafetyError as exc:
            policy = classify_expected_demo_policy_rejection(exc)
            if policy is None:
                raise
            rejected_at = datetime.now(timezone.utc)
            self.portfolio.release(
                reservation.id, activate_cooldown=False
            )
            candidate.admitted = False
            candidate.state = "EXECUTION_REJECTED"
            candidate.rejection_reason = str(exc)
            candidate.execution_rejected_at = rejected_at
            self.repository.save_v2_signal_candidate(candidate)
            self.last_error = str(exc)
            return {
                "execution_attempted": False,
                "candidate_id": str(candidate.id),
                "reservation_id": str(reservation.id),
                "execution_id": None,
                "state": candidate.state,
                "symbol": candidate.symbol.value,
                "strategy": candidate.strategy_name.value,
                "rejection_code": policy.code,
                "rejection_message": str(exc),
                "risk_control": policy.risk_control,
                "processing_stage": "demo_execution",
                "rejected_at": rejected_at.isoformat(),
                "handled_policy_rejection": True,
                "exchange_mutation_performed": False,
                "exchange_order_submitted": False,
                "exchange_environment": "BYBIT_DEMO",
            }
        if record is None:
            self.last_error = self.demo_execution.last_error or "Demo execution was blocked"
            self.portfolio.release(reservation.id, activate_cooldown=False)
            return self._blocked(candidate, self.last_error)
        self.portfolio.mark_open(reservation.id, record.id)
        candidate.state = record.state.value
        self.repository.save_v2_signal_candidate(candidate)
        return {
            "execution_attempted": True, "candidate_id": str(candidate.id),
            "execution_id": str(record.id), "state": record.state.value,
            "symbol": candidate.symbol.value, "quantity": str(quantity),
            "notional": str(notional), "leverage": str(leverage),
            "exchange_environment": "BYBIT_DEMO",
        }

    def _persist_compatibility_candidate(
        self, candidate: V2SignalCandidate, quantity: Decimal, notional: Decimal,
    ) -> tuple[SignalDryRunResult, NewsClassification] | None:
        now = datetime.now(timezone.utc)
        news_id = uuid5(NAMESPACE_URL, f"bybot-v2-signal:{candidate.id}")
        asset = Asset.BTC if candidate.symbol.value == "BTCUSDT" else Asset.ETH if candidate.symbol.value == "ETHUSDT" else Asset.MARKET
        sentiment = "BULLISH" if candidate.side == StrategySide.LONG else "BEARISH"
        news = NewsItem(
            id=news_id, title=f"{candidate.strategy_name.value} {candidate.symbol.value}",
            # This is an execution compatibility record, not a deduplicated
            # external news event. Include the durable candidate identity so
            # repeated strategy setups across runs cannot share a content hash.
            summary=f"{candidate.entry_reason} [candidate_id={candidate.id}]",
            source="bybot-v2-deterministic-strategy",
            published_at=candidate.created_at, received_at=now, asset_hint=asset,
            importance=float(candidate.confidence),
        )
        classification = NewsClassification(
            news_id=news.id, asset=asset, sentiment=sentiment,
            confidence=float(candidate.confidence), category="other", urgency="normal",
            reason="deterministic V2 strategy; no LLM market data",
            classification_status=ClassificationStatus.SUCCESS,
            trade_eligible=True, provider_name="deterministic-v2",
            model_name="deterministic-v2",
            classifier_version=candidate.strategy_version, classified_at=now,
        )
        action = NewsSignalAction.BUY if candidate.side == StrategySide.LONG else NewsSignalAction.SELL
        v1_candidate = NewsSignalCandidate(
            id=candidate.id, news_id=news.id,
            execution_environment=ExecutionEnvironment.BYBIT_DEMO,
            run_id=self.run_id, symbol=candidate.symbol,
            state=CandidateLifecycleState.READY,
            proposed_action=action, final_action=action,
            sentiment=classification.sentiment,
            classification_confidence=float(candidate.confidence),
            news_importance=float(candidate.confidence), category="other", urgency="normal",
            market_confirmation=MarketConfirmation(
                available=True, fresh=True, direction_confirmed=True,
                price_change_1m_pct=float(candidate.feature_snapshot.price_momentum.get("1m", 0) / Decimal("100")),
                trend_direction=("bullish" if candidate.side == StrategySide.LONG else "bearish"),
                trend_score=float(candidate.raw_strategy_score),
                spread_bps=float(candidate.feature_snapshot.spread_bps),
                volatility_pct=float(candidate.feature_snapshot.realized_volatility.get("1m", 0) / Decimal("100")),
                volume_24h=float(candidate.feature_snapshot.volume_24h),
                reasons=[candidate.entry_reason],
            ),
            expected_edge_bps=float(candidate.estimated_edge_bps),
            proposed_stop_loss_pct=float(candidate.stop_loss_pct),
            proposed_take_profit_pct=float(candidate.take_profit_pct),
            ttl_seconds=max(1, int((candidate.expires_at - candidate.created_at).total_seconds())),
            reasons=[candidate.entry_reason], created_at=candidate.created_at,
            expires_at=candidate.expires_at,
        )
        fees = notional * self.settings.v2_taker_fee_bps * Decimal("2") / Decimal("10000")
        slippage = notional * self.settings.v2_slippage_bps * Decimal("2") / Decimal("10000")
        decision = RiskDecision(
            approved=True, capped_size=float(quantity), position_notional=float(notional),
            max_allowed_notional=float(notional), estimated_fees=float(fees),
            estimated_slippage=float(slippage), reasons=[],
        )
        result = SignalDryRunResult(candidate=v1_candidate, risk_preview=SignalRiskPreview())
        risk_id = self.repository.persist_v2_compatibility_bundle(
            news,
            classification,
            result,
            decision,
            classifier_version=candidate.strategy_version,
            cache_expires_at=now + timedelta(days=1),
        )
        if risk_id is None:
            return None
        return result, classification

    def _persistence_blocked(self, candidate: V2SignalCandidate) -> dict[str, Any]:
        error_code = self.repository.last_error_code or "DB_COMPATIBILITY_BUNDLE_FAILED"
        reason = "durable execution candidate could not be persisted"
        candidate.state = "PERSISTENCE_BLOCKED"
        candidate.rejection_reason = reason
        self.repository.save_v2_signal_candidate(candidate)
        self.last_error = f"{reason} ({error_code})"
        return {
            "execution_attempted": False,
            "candidate_id": str(candidate.id),
            "state": candidate.state,
            "reason": reason,
            "rejection_code": error_code,
            "processing_stage": "compatibility_persistence",
            "handled_persistence_rejection": True,
            "exchange_environment": "BYBIT_DEMO",
            "exchange_order_submitted": False,
        }

    def _blocked(self, candidate: V2SignalCandidate, reason: str) -> dict[str, Any]:
        candidate.state = "EXECUTION_BLOCKED"
        candidate.rejection_reason = reason
        self.repository.save_v2_signal_candidate(candidate)
        self.last_error = reason
        return {
            "execution_attempted": False, "candidate_id": str(candidate.id),
            "state": candidate.state, "reason": reason,
            "exchange_environment": "BYBIT_DEMO", "exchange_order_submitted": False,
        }


def _market_snapshot(candidate: V2SignalCandidate) -> MarketSnapshot:
    feature = candidate.feature_snapshot
    momentum_pct = float(feature.price_momentum.get("1m", Decimal("0")) / Decimal("100"))
    return MarketSnapshot(
        symbol=feature.symbol, last_price=float(feature.last_price),
        bid_price=float(feature.bid_price), ask_price=float(feature.ask_price),
        spread=float(feature.ask_price - feature.bid_price),
        spread_pct=float(feature.spread_bps / Decimal("100")),
        price_change_1m_pct=momentum_pct,
        simple_trend=(SimpleTrend.BULLISH if momentum_pct > 0 else SimpleTrend.BEARISH if momentum_pct < 0 else SimpleTrend.SIDEWAYS),
        simple_volatility=float(feature.realized_volatility.get("1m", Decimal("0")) / Decimal("100")),
        volume_24h=float(feature.volume_24h), timestamp=feature.timestamp,
        trend_score=float(max(Decimal("-1"), min(Decimal("1"), feature.price_momentum.get("1m", Decimal("0")) / Decimal("100")))),
        volatility_pct=float(feature.realized_volatility.get("1m", Decimal("0")) / Decimal("100")),
        liquidity_ok=feature.spread_bps <= Decimal("15"), api_stable=feature.fresh,
    )
