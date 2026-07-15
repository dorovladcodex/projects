from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from threading import RLock
from typing import Any, Iterator

from app.config import Settings
from app.models import Symbol
from app.v2.models import (
    PortfolioReservation, ReservationState, StrategyName, UniverseInstrument,
)
from app.v2.strategies import MEME_SYMBOLS


CORRELATION_GROUPS: dict[str, set[Symbol]] = {
    "market_core": {Symbol.BTCUSDT, Symbol.ETHUSDT},
    "solana_ecosystem": {Symbol.SOLUSDT, Symbol.WIFUSDT, Symbol.BONKUSDT},
    "large_cap_alts": {
        Symbol.XRPUSDT, Symbol.ADAUSDT, Symbol.LINKUSDT, Symbol.AVAXUSDT,
        Symbol.SUIUSDT, Symbol.NEARUSDT, Symbol.LTCUSDT, Symbol.TONUSDT,
    },
    "memes": {
        Symbol.DOGEUSDT, Symbol.PEPEUSDT, Symbol.SHIBUSDT, Symbol.WIFUSDT,
        Symbol.BONKUSDT, Symbol.FLOKIUSDT,
    },
}


def correlation_group(symbol: Symbol) -> str:
    for name, symbols in CORRELATION_GROUPS.items():
        if symbol in symbols:
            return name
    return "other"


class PortfolioRiskService:
    """Restart-safe admission/reservation layer; it never calls an exchange."""

    ACTIVE = {ReservationState.RESERVED, ReservationState.EXECUTING, ReservationState.OPEN}

    def __init__(self, settings: Settings, repository: Any | None = None) -> None:
        self.settings = settings
        self.repository = repository
        self._global_lock = RLock()
        self._symbol_locks: dict[Symbol, RLock] = defaultdict(RLock)
        self.reservations: list[PortfolioReservation] = []
        self.symbol_cooldown_until: dict[Symbol, datetime] = {}
        self.last_entry_at: datetime | None = None
        self.kill_switch_active = False
        self.kill_switch_reasons: list[str] = []
        self.current_drawdown_pct = Decimal("0")
        self.daily_pnl = Decimal("0")
        self.weekly_pnl = Decimal("0")
        self.restore()

    def restore(self) -> None:
        loader = getattr(self.repository, "load_v2_portfolio_state", None)
        if not callable(loader):
            return
        state = loader()
        if not state:
            return
        self.reservations = [PortfolioReservation.model_validate(item) for item in state.get("reservations", [])]
        now = datetime.now(timezone.utc)
        self.symbol_cooldown_until = {
            Symbol(key): datetime.fromisoformat(value)
            for key, value in (state.get("symbol_cooldowns") or {}).items()
            if datetime.fromisoformat(value) > now
        }
        self.last_entry_at = datetime.fromisoformat(state["last_entry_at"]) if state.get("last_entry_at") else None
        self.kill_switch_active = bool(state.get("kill_switch_active"))
        self.kill_switch_reasons = list(state.get("kill_switch_reasons") or [])
        self.daily_pnl = Decimal(str(state.get("daily_pnl", "0")))
        self.weekly_pnl = Decimal(str(state.get("weekly_pnl", "0")))
        self.current_drawdown_pct = Decimal(str(state.get("current_drawdown_pct", "0")))

    @contextmanager
    def symbol_lock(self, symbol: Symbol) -> Iterator[None]:
        with self._symbol_locks[symbol]:
            yield

    def block_reasons(
        self, symbol: Symbol, notional: Decimal, *, risk_usdt: Decimal | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        current = now or datetime.now(timezone.utc)
        active = [item for item in self.reservations if item.state in self.ACTIVE]
        reasons: list[str] = []
        if self.kill_switch_active:
            reasons.append("portfolio kill switch is active")
        if sum(item.symbol == symbol for item in active) >= self.settings.max_positions_per_symbol:
            reasons.append("maximum positions per symbol reached")
        if len(active) >= self.settings.max_concurrent_positions:
            reasons.append("maximum concurrent positions reached")
        if symbol in MEME_SYMBOLS and sum(item.symbol in MEME_SYMBOLS for item in active) >= self.settings.max_meme_positions:
            reasons.append("maximum meme positions reached")
        group = correlation_group(symbol)
        if sum(item.correlation_group == group for item in active) >= self.settings.max_positions_per_correlation_group:
            reasons.append("maximum positions for correlation group reached")
        if sum(item.notional_usdt for item in active) + notional > self.settings.max_total_notional_usdt:
            reasons.append("maximum total notional reached")
        projected_risk = sum(item.risk_usdt for item in active) + (risk_usdt or Decimal("0"))
        if risk_usdt is not None and projected_risk > self.settings.risk_capital_usdt * self.settings.max_portfolio_risk_pct / Decimal("100"):
            reasons.append("maximum portfolio risk reached")
        cooldown = self.symbol_cooldown_until.get(symbol)
        if cooldown and cooldown > current:
            reasons.append("symbol cooldown is active")
        if self.last_entry_at and current - self.last_entry_at < timedelta(seconds=self.settings.v2_global_entry_cooldown_seconds):
            reasons.append("global entry cooldown is active")
        recent = [item for item in self.reservations if current - item.created_at <= timedelta(minutes=5)]
        if len(recent) >= self.settings.max_new_entries_per_5_minutes:
            reasons.append("five-minute entry rate limit reached")
        today = [item for item in self.reservations if item.created_at.date() == current.date()]
        if len(today) >= self.settings.max_trades_per_day:
            reasons.append("daily trade cap reached")
        return reasons

    def reserve(
        self, *, run_id: str, candidate_id: Any, symbol: Symbol,
        strategy_name: StrategyName, notional: Decimal, risk_usdt: Decimal,
    ) -> PortfolioReservation | None:
        with self._global_lock, self.symbol_lock(symbol):
            if self.block_reasons(symbol, notional, risk_usdt=risk_usdt):
                return None
            reservation = PortfolioReservation(
                run_id=run_id, candidate_id=candidate_id, symbol=symbol,
                strategy_name=strategy_name, correlation_group=correlation_group(symbol),
                notional_usdt=notional, risk_usdt=risk_usdt,
            )
            saver = getattr(self.repository, "reserve_v2_portfolio", None)
            if callable(saver):
                durable = saver(reservation, self.settings)
                if durable is None:
                    return None
                reservation = durable
            self.reservations.append(reservation)
            self.last_entry_at = reservation.created_at
            self._persist_state()
            return reservation

    def mark_open(self, reservation_id: Any, execution_id: Any) -> None:
        self._transition(reservation_id, ReservationState.OPEN, execution_id=execution_id)

    def release(self, reservation_id: Any, *, closed_at: datetime | None = None) -> None:
        current = closed_at or datetime.now(timezone.utc)
        item = self._transition(reservation_id, ReservationState.RELEASED)
        if item:
            item.released_at = current
            self.symbol_cooldown_until[item.symbol] = current + timedelta(seconds=self.settings.v2_symbol_cooldown_seconds)
            updater = getattr(self.repository, "update_v2_portfolio_reservation", None)
            if callable(updater):
                updater(item)
            self._persist_state()

    def apply_realized_pnl(self, pnl: Decimal, peak_equity: Decimal, equity: Decimal) -> None:
        self.daily_pnl += pnl
        self.weekly_pnl += pnl
        self.current_drawdown_pct = (
            (peak_equity - equity) / peak_equity * Decimal("100") if peak_equity > 0 else Decimal("0")
        )
        capital = self.settings.risk_capital_usdt
        reasons = []
        if self.daily_pnl <= -(capital * self.settings.v2_max_daily_loss_pct / 100):
            reasons.append("maximum daily net loss reached")
        if self.weekly_pnl <= -(capital * self.settings.v2_max_weekly_loss_pct / 100):
            reasons.append("maximum weekly net loss reached")
        if self.current_drawdown_pct >= self.settings.v2_max_drawdown_pct:
            reasons.append("maximum portfolio drawdown reached")
        if reasons:
            self.kill_switch_active = True
            self.kill_switch_reasons.extend(reason for reason in reasons if reason not in self.kill_switch_reasons)
        self._persist_state()

    def _transition(
        self, reservation_id: Any, state: ReservationState, *, execution_id: Any = None,
    ) -> PortfolioReservation | None:
        with self._global_lock:
            item = next((row for row in self.reservations if str(row.id) == str(reservation_id)), None)
            if item is None:
                return None
            item.state = state
            if execution_id is not None:
                item.execution_id = execution_id
            updater = getattr(self.repository, "update_v2_portfolio_reservation", None)
            if callable(updater):
                updater(item)
            return item

    def _persist_state(self) -> None:
        saver = getattr(self.repository, "save_v2_portfolio_state", None)
        if callable(saver):
            saver({
                "symbol_cooldowns": {key.value: value.isoformat() for key, value in self.symbol_cooldown_until.items()},
                "last_entry_at": self.last_entry_at.isoformat() if self.last_entry_at else None,
                "kill_switch_active": self.kill_switch_active,
                "kill_switch_reasons": list(self.kill_switch_reasons),
                "daily_pnl": str(self.daily_pnl), "weekly_pnl": str(self.weekly_pnl),
                "current_drawdown_pct": str(self.current_drawdown_pct),
            })


def normalize_leverage(requested: Decimal, instrument: UniverseInstrument) -> Decimal:
    bounded = min(max(requested, instrument.min_leverage), instrument.max_leverage)
    steps = ((bounded - instrument.min_leverage) / instrument.leverage_step).to_integral_value(rounding=ROUND_DOWN)
    return instrument.min_leverage + steps * instrument.leverage_step


def normalize_order_quantity(
    target_notional: Decimal, price: Decimal, instrument: UniverseInstrument,
    max_notional: Decimal,
) -> Decimal:
    if target_notional > max_notional:
        target_notional = max_notional
    minimum_for_notional = (
        instrument.min_notional_value / price / instrument.qty_step
    ).to_integral_value(rounding=ROUND_UP) * instrument.qty_step
    minimum = max(instrument.min_order_qty, minimum_for_notional)
    desired = (target_notional / price / instrument.qty_step).to_integral_value(rounding=ROUND_DOWN) * instrument.qty_step
    quantity = max(minimum, desired)
    if quantity * price > max_notional:
        raise ValueError("exchange minimum quantity exceeds configured maximum notional")
    return quantity
