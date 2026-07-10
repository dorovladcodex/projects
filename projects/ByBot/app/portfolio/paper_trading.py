from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import (
    MarketSnapshot,
    PaperPnl,
    PaperPosition,
    PositionStatus,
    RiskDecision,
    Side,
    TradeSignal,
)


class PaperTradingService:
    """In-memory paper trading loop. It never calls Bybit order endpoints."""

    def __init__(self, *, timeout: timedelta = timedelta(minutes=60)) -> None:
        self.timeout = timeout
        self.positions: list[PaperPosition] = []
        self.closed_trades: list[PaperPosition] = []
        self.last_risk_decision: RiskDecision | None = None
        self.last_error: str | None = None

    @property
    def open_position(self) -> PaperPosition | None:
        return next((position for position in self.positions if position.status == PositionStatus.OPEN), None)

    @property
    def status(self) -> str:
        return "OPEN_POSITION" if self.open_position else "IDLE"

    def open_from_signal(
        self,
        signal: TradeSignal,
        risk_decision: RiskDecision,
        market: MarketSnapshot,
        *,
        take_profit_pct: float,
    ) -> PaperPosition:
        self.last_risk_decision = risk_decision
        if not risk_decision.approved:
            self.last_error = "risk manager rejected signal"
            raise PermissionError(self.last_error)
        if self.open_position is not None:
            self.last_error = "maximum open paper positions reached"
            raise RuntimeError(self.last_error)
        if signal.side is None:
            self.last_error = "signal side is required"
            raise ValueError(self.last_error)
        if signal.stop_loss_pct is None:
            self.last_error = "stop loss is mandatory"
            raise ValueError(self.last_error)

        entry_price = market.ask_price if signal.side == Side.BUY else market.bid_price
        stop_distance = entry_price * signal.stop_loss_pct / 100
        if stop_distance <= 0:
            self.last_error = "stop loss distance must be positive"
            raise ValueError(self.last_error)
        size = risk_decision.max_loss_amount / stop_distance
        if size <= 0:
            self.last_error = "position size must be positive"
            raise ValueError(self.last_error)

        stop_loss = (
            entry_price - stop_distance
            if signal.side == Side.BUY
            else entry_price + stop_distance
        )
        take_profit_distance = entry_price * take_profit_pct / 100
        take_profit = (
            entry_price + take_profit_distance
            if signal.side == Side.BUY
            else entry_price - take_profit_distance
        )
        position = PaperPosition(
            symbol=signal.symbol,
            side=signal.side,
            size=size,
            entry_price=entry_price,
            current_price=market.last_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            unrealized_pnl=_calculate_pnl(signal.side, size, entry_price, market.last_price),
            reason="opened_by_test_signal",
        )
        self.positions.append(position)
        self.last_error = None
        return position

    def update_from_market(
        self,
        market: MarketSnapshot,
        *,
        now: datetime | None = None,
    ) -> PaperPosition | None:
        position = self.open_position
        if position is None or position.symbol != market.symbol:
            return position

        now = now or datetime.now(timezone.utc)
        position.current_price = market.last_price
        position.unrealized_pnl = _calculate_pnl(
            position.side,
            position.size,
            position.entry_price,
            market.last_price,
        )

        if _stop_loss_hit(position):
            return self.close_position(market.last_price, reason="stop_loss", now=now)
        if _take_profit_hit(position):
            return self.close_position(market.last_price, reason="take_profit", now=now)
        if now - position.opened_at >= self.timeout:
            return self.close_position(market.last_price, reason="timeout", now=now)
        return position

    def close_position(
        self,
        exit_price: float,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> PaperPosition:
        allowed_reasons = {"manual_close", "stop_loss", "take_profit", "timeout"}
        if reason not in allowed_reasons:
            self.last_error = f"unsupported close reason: {reason}"
            raise ValueError(self.last_error)
        position = self.open_position
        if position is None:
            self.last_error = "no open paper position"
            raise RuntimeError(self.last_error)
        now = now or datetime.now(timezone.utc)
        position.current_price = exit_price
        position.realized_pnl = _calculate_pnl(
            position.side,
            position.size,
            position.entry_price,
            exit_price,
        )
        position.unrealized_pnl = 0.0
        position.status = PositionStatus.CLOSED
        position.closed_at = now
        position.reason = reason
        self.closed_trades.append(position)
        self.last_error = None
        return position

    def pnl(self) -> PaperPnl:
        open_position = self.open_position
        unrealized = open_position.unrealized_pnl if open_position else 0.0
        realized = sum(position.realized_pnl for position in self.closed_trades)
        return PaperPnl(
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            total_pnl=realized + unrealized,
            open_positions=1 if open_position else 0,
            closed_trades=len(self.closed_trades),
        )

    def positions_payload(self) -> list[dict[str, object]]:
        return [
            position.model_dump(mode="json")
            for position in self.positions
            if position.status == PositionStatus.OPEN
        ]

    def trades_payload(self) -> list[dict[str, object]]:
        return [position.model_dump(mode="json") for position in self.closed_trades]

    def as_status(self) -> dict[str, object]:
        return {
            "status": self.status,
            "open_position": (
                self.open_position.model_dump(mode="json") if self.open_position else None
            ),
            "realized_pnl": self.pnl().realized_pnl,
            "unrealized_pnl": self.pnl().unrealized_pnl,
            "last_trade": (
                self.closed_trades[-1].model_dump(mode="json") if self.closed_trades else None
            ),
            "last_risk_decision": (
                self.last_risk_decision.model_dump(mode="json")
                if self.last_risk_decision
                else None
            ),
            "last_error": self.last_error,
        }


def _calculate_pnl(side: Side, size: float, entry_price: float, current_price: float) -> float:
    if side == Side.BUY:
        return (current_price - entry_price) * size
    return (entry_price - current_price) * size


def _stop_loss_hit(position: PaperPosition) -> bool:
    if position.side == Side.BUY:
        return position.current_price <= position.stop_loss
    return position.current_price >= position.stop_loss


def _take_profit_hit(position: PaperPosition) -> bool:
    if position.side == Side.BUY:
        return position.current_price >= position.take_profit
    return position.current_price <= position.take_profit
