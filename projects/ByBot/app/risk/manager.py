from __future__ import annotations

from dataclasses import dataclass

from app.models import MarketSnapshot, RiskContext, RiskDecision, SignalAction, TradeSignal


@dataclass(frozen=True)
class RiskRules:
    max_open_positions: int = 1
    max_risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_weekly_loss_pct: float = 5.0
    max_consecutive_losses: int = 3
    max_leverage: int = 2
    max_spread_bps: float = 8.0
    min_confidence: float = 0.7
    min_expected_edge_bps: float = 12.0


class RiskManager:
    """Final, deterministic authority. Approval is required before execution."""

    def __init__(self, rules: RiskRules | None = None) -> None:
        self.rules = rules or RiskRules()

    def assess(
        self, signal: TradeSignal, market: MarketSnapshot, context: RiskContext
    ) -> RiskDecision:
        reasons: list[str] = []
        if signal.action != SignalAction.TRADE:
            reasons.append("strategy did not produce a trade")
        if signal.side is None:
            reasons.append("trade side is missing")
        if signal.stop_loss_pct is None:
            reasons.append("stop loss is mandatory")
        if context.open_positions >= self.rules.max_open_positions:
            reasons.append("maximum open positions reached")
        if context.requested_risk_pct > self.rules.max_risk_per_trade_pct:
            reasons.append("risk per trade exceeds limit")
        if context.daily_pnl_pct <= -self.rules.max_daily_loss_pct:
            reasons.append("daily loss limit reached")
        if context.weekly_pnl_pct <= -self.rules.max_weekly_loss_pct:
            reasons.append("weekly loss limit reached")
        if context.consecutive_losses >= self.rules.max_consecutive_losses:
            reasons.append("paused after consecutive losses")
        if context.leverage > self.rules.max_leverage:
            reasons.append("leverage exceeds limit")
        if not context.api_stable or not market.api_stable:
            reasons.append("API or WebSocket instability")
        if not market.liquidity_ok:
            reasons.append("liquidity is insufficient")
        if market.spread_bps > self.rules.max_spread_bps:
            reasons.append("spread is too wide")
        if signal.confidence < self.rules.min_confidence:
            reasons.append("signal confidence is too low")
        if signal.expected_edge_bps < self.rules.min_expected_edge_bps:
            reasons.append("expected edge after costs is too small")

        approved = not reasons
        max_loss = context.equity * context.requested_risk_pct / 100 if approved else 0
        return RiskDecision(approved=approved, reasons=reasons, max_loss_amount=max_loss)
