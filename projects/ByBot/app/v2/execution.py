from __future__ import annotations

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
        reservation = self.portfolio.reserve(
            run_id=self.run_id, candidate_id=candidate.id, symbol=candidate.symbol,
            strategy_name=candidate.strategy_name, notional=notional,
            risk_usdt=risk_usdt,
        )
        if reservation is None:
            return self._blocked(candidate, "durable portfolio reservation was rejected")
        compatibility = self._persist_compatibility_candidate(candidate, quantity, notional)
        if compatibility is None:
            self.portfolio.release(reservation.id)
            return self._blocked(candidate, "durable execution candidate could not be persisted")
        result, classification = compatibility
        reservation.state = ReservationState.EXECUTING
        self.repository.update_v2_portfolio_reservation(reservation)
        leverage = normalize_leverage(
            self.settings.v2_leverage_for_symbol(candidate.symbol.value),
            universe_status.instrument,
        )
        record = self.demo_execution.submit_candidate(
            result.candidate, result.risk_preview, classification,
            _market_snapshot(candidate), desired_leverage=leverage,
            strategy_name=candidate.strategy_name.value,
            strategy_version=candidate.strategy_version,
            trailing_stop_pct=candidate.trailing_stop_pct,
            break_even_at_r=candidate.break_even_at_r,
            maximum_holding_seconds=candidate.maximum_holding_seconds,
        )
        if record is None:
            self.last_error = self.demo_execution.last_error or "Demo execution was blocked"
            self.portfolio.release(reservation.id)
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
            summary=candidate.entry_reason, source="bybot-v2-deterministic-strategy",
            published_at=candidate.created_at, received_at=now, asset_hint=asset,
            importance=float(candidate.confidence),
        )
        classification = NewsClassification(
            news_id=news.id, asset=asset, sentiment=sentiment,
            confidence=float(candidate.confidence), category="other", urgency="normal",
            reason="deterministic V2 strategy; no LLM market data",
            classification_status=ClassificationStatus.SUCCESS,
            trade_eligible=True, provider_name="deterministic-v2",
            model_name=candidate.strategy_name.value,
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
        if not self.repository.save_news(news):
            existing = self.repository.load_news()[0]
            if not any(item.id == news.id for item in existing):
                return None
        self.repository.save_classification(news, classification, candidate.strategy_version, now + timedelta(days=1))
        result = SignalDryRunResult(candidate=v1_candidate, risk_preview=SignalRiskPreview())
        self.repository.save_signal_result(result)
        risk_id = self.repository.save_risk_decision(str(candidate.id), decision)
        if risk_id is None:
            return None
        result.risk_preview = SignalRiskPreview(
            preview_performed=True, approved=True, capped_size=float(quantity),
            position_notional=float(notional), max_allowed_notional=float(notional),
            rejection_reasons=[], risk_decision_id=risk_id,
            estimated_fees=float(fees), estimated_slippage=float(slippage),
        )
        self.repository.save_signal_result(result)
        return result, classification

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
