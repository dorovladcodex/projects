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
    max_position_notional_usdt: float = 5_000.0
    max_position_notional_pct_of_equity: float = 5.0
    min_position_notional_usdt: float = 10.0
    default_paper_fees_bps: float = 6.0
    default_slippage_bps: float = 2.0
    min_net_edge_bps: float = 5.0


class RiskManager:
    """Final, deterministic authority. Approval is required before execution."""

    def __init__(self, rules: RiskRules | None = None) -> None:
        self.rules = rules or RiskRules()

    def assess(
        self, signal: TradeSignal, market: MarketSnapshot, context: RiskContext
    ) -> RiskDecision:
        reasons: list[str] = []
        execution_price = market.ask_price if signal.side and signal.side.value == "BUY" else market.bid_price
        max_loss = context.equity * context.requested_risk_pct / 100
        risk_based_size = 0.0
        capped_size = 0.0
        position_notional = 0.0
        max_allowed_notional = 0.0
        estimated_fees = 0.0
        estimated_slippage = 0.0
        size_was_capped = False
        take_profit_bps = signal.take_profit_pct * 100 if signal.take_profit_pct else 0.0
        stop_loss_bps = signal.stop_loss_pct * 100 if signal.stop_loss_pct else 0.0
        round_trip_fee_bps = self.rules.default_paper_fees_bps * 2
        round_trip_slippage_bps = self.rules.default_slippage_bps * 2
        round_trip_cost_bps = round_trip_fee_bps + round_trip_slippage_bps
        effective_expected_edge_bps = signal.expected_edge_bps
        expected_net_edge_bps = effective_expected_edge_bps - round_trip_cost_bps

        effective_leverage = min(context.leverage, self.rules.max_leverage)
        equity_notional_cap = context.equity * effective_leverage
        available_notional_cap = (
            context.available_balance * effective_leverage
            if context.available_balance is not None
            else equity_notional_cap
        )
        pct_equity_cap = context.equity * self.rules.max_position_notional_pct_of_equity / 100
        max_allowed_notional = min(
            self.rules.max_position_notional_usdt,
            equity_notional_cap,
            available_notional_cap,
            pct_equity_cap,
        )

        if signal.stop_loss_pct is not None and signal.side is not None:
            stop_distance = execution_price * signal.stop_loss_pct / 100
            if stop_distance > 0:
                risk_based_size = max_loss / stop_distance
                risk_based_notional = risk_based_size * execution_price
                capped_notional = min(risk_based_notional, max_allowed_notional)
                capped_size = capped_notional / execution_price if execution_price > 0 else 0.0
                position_notional = capped_size * execution_price
                estimated_fees = (
                    position_notional * self.rules.default_paper_fees_bps / 10_000 * 2
                )
                estimated_slippage = (
                    position_notional * self.rules.default_slippage_bps / 10_000 * 2
                )
                size_was_capped = capped_size < risk_based_size

        if signal.action != SignalAction.TRADE:
            reasons.append("strategy did not produce a trade")
        if signal.side is None:
            reasons.append("trade side is missing")
        if signal.stop_loss_pct is None:
            reasons.append("stop loss is mandatory")
        if signal.take_profit_pct is None:
            reasons.append("take profit is mandatory")
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
        if take_profit_bps and signal.expected_edge_bps > take_profit_bps:
            reasons.append("expected edge is inconsistent with take profit target")
        if take_profit_bps and (
            take_profit_bps <= round_trip_cost_bps + self.rules.min_net_edge_bps
        ):
            reasons.append("take profit is smaller than fees/slippage plus required net edge")
        if max_allowed_notional < self.rules.min_position_notional_usdt:
            reasons.append("max allowed notional is below minimum position notional")
        if position_notional and position_notional < self.rules.min_position_notional_usdt:
            reasons.append("position notional is below minimum")
        total_cost_bps = (self.rules.default_paper_fees_bps + self.rules.default_slippage_bps) * 2
        if signal.expected_edge_bps <= total_cost_bps:
            reasons.append("expected edge after fees and slippage is too small")

        approved = not reasons
        return RiskDecision(
            approved=approved,
            reasons=reasons,
            max_loss_amount=max_loss if approved else 0,
            risk_based_size=risk_based_size,
            capped_size=capped_size,
            position_notional=position_notional,
            max_allowed_notional=max_allowed_notional,
            estimated_fees=estimated_fees,
            estimated_slippage=estimated_slippage,
            size_was_capped=size_was_capped,
            take_profit_bps=take_profit_bps,
            stop_loss_bps=stop_loss_bps,
            round_trip_cost_bps=round_trip_cost_bps,
            min_net_edge_bps=self.rules.min_net_edge_bps,
            effective_expected_edge_bps=effective_expected_edge_bps,
            expected_net_edge_bps=expected_net_edge_bps,
        )
