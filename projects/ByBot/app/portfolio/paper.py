from __future__ import annotations

from app.config import BotMode
from app.models import MarketSnapshot, PaperOrder, Position, RiskDecision, Side, TradeSignal


class PaperExecutionEngine:
    def __init__(self, mode: BotMode = BotMode.PAPER) -> None:
        if mode != BotMode.PAPER:
            raise ValueError("PaperExecutionEngine can only run in PAPER mode")
        self.orders: list[PaperOrder] = []
        self.position: Position | None = None

    def execute(
        self,
        signal: TradeSignal,
        risk: RiskDecision,
        market: MarketSnapshot,
    ) -> PaperOrder:
        if not risk.approved:
            raise PermissionError("RiskManager rejected the trade")
        if self.position is not None:
            raise RuntimeError("Only one open position is allowed")
        if signal.side is None or signal.stop_loss_pct is None:
            raise ValueError("Executable signal requires side and stop loss")

        fill_price = market.ask_price if signal.side == Side.BUY else market.bid_price
        stop_distance = fill_price * signal.stop_loss_pct / 100
        quantity = risk.max_loss_amount / stop_distance
        stop_price = (
            fill_price - stop_distance if signal.side == Side.BUY else fill_price + stop_distance
        )
        order = PaperOrder(
            symbol=signal.symbol,
            side=signal.side,
            quantity=quantity,
            fill_price=fill_price,
            stop_loss_price=stop_price,
        )
        self.orders.append(order)
        self.position = Position(
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            entry_price=order.fill_price,
            stop_loss_price=order.stop_loss_price,
        )
        return order
