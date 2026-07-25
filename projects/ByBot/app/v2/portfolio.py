from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from threading import RLock
from typing import Any, Iterator

from app.config import Settings
from app.models import DemoExecutionRecord, Side, Symbol
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
        self.cumulative_realized_pnl = Decimal("0")
        self.unrealized_pnl = Decimal("0")
        self.equity = settings.risk_capital_usdt
        self.peak_equity = settings.risk_capital_usdt
        self.realized_events: dict[str, dict[str, str]] = {}
        self.restore()

    def restore(self) -> None:
        loader = getattr(self.repository, "load_v2_portfolio_state", None)
        if not callable(loader):
            return
        state = loader()
        if not state:
            return
        self.reservations = [PortfolioReservation.model_validate(item) for item in state.get("reservations", [])]
        executions = (
            self.repository.load_demo_executions()
            if callable(getattr(self.repository, "load_demo_executions", None))
            else []
        )
        executions_by_candidate = {
            str(item.candidate_id): item for item in executions
        }
        executions_by_id = {str(item.id): item for item in executions}
        # A Demo order cannot exist without a durable DemoExecution reservation.
        # Therefore a restored V2 reservation with neither execution_id nor a
        # matching DemoExecution is a safe pre-submit orphan and must not block
        # the symbol after restart.
        recovered_reservation = False
        for reservation in self.reservations:
            if reservation.state not in self.ACTIVE:
                continue
            execution = (
                executions_by_id.get(str(reservation.execution_id))
                if reservation.execution_id is not None
                else executions_by_candidate.get(str(reservation.candidate_id))
            )
            if execution is not None:
                reservation.execution_id = execution.id
                if execution.state.value in {
                    "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE",
                    "DEMO_CLOSED_AFTER_INTERRUPTION", "DEMO_CLOSED_EXTERNALLY",
                    "DEMO_FAILED_FLAT_VERIFIED", "DEMO_NOT_SUBMITTED",
                    "DEMO_ORDER_CANCELLED",
                }:
                    reservation.state = ReservationState.RELEASED
                    reservation.released_at = execution.closed_at or execution.updated_at
                else:
                    reservation.state = ReservationState.OPEN
            elif reservation.execution_id is None:
                reservation.state = ReservationState.RELEASED
                reservation.released_at = datetime.now(timezone.utc)
            else:
                # An execution ID without its durable execution row is an
                # unresolved persistence inconsistency and remains reserved.
                continue
            recovered_reservation = True
            updater = getattr(self.repository, "update_v2_portfolio_reservation", None)
            if callable(updater):
                updater(reservation)
        now = datetime.now(timezone.utc)
        self.symbol_cooldown_until = {
            Symbol(key): datetime.fromisoformat(value)
            for key, value in (state.get("symbol_cooldowns") or {}).items()
            if datetime.fromisoformat(value) > now
        }
        actual_entries = [
            item.created_at for item in self.reservations if item.execution_id is not None
        ]
        self.last_entry_at = max(actual_entries) if actual_entries else None
        self.kill_switch_active = bool(state.get("kill_switch_active"))
        self.kill_switch_reasons = list(state.get("kill_switch_reasons") or [])
        self.daily_pnl = Decimal(str(state.get("daily_pnl", "0")))
        self.weekly_pnl = Decimal(str(state.get("weekly_pnl", "0")))
        self.current_drawdown_pct = Decimal(str(state.get("current_drawdown_pct", "0")))
        self.cumulative_realized_pnl = Decimal(
            str(state.get("cumulative_realized_pnl", "0"))
        )
        self.unrealized_pnl = Decimal(str(state.get("unrealized_pnl", "0")))
        self.equity = Decimal(
            str(state.get("equity", self.settings.risk_capital_usdt))
        )
        self.peak_equity = Decimal(
            str(state.get("peak_equity", max(self.settings.risk_capital_usdt, self.equity)))
        )
        stored_capital = Decimal(
            str(
                state.get(
                    "risk_capital_usdt",
                    self.equity
                    - self.cumulative_realized_pnl
                    - self.unrealized_pnl,
                )
            )
        )
        capital_rebased = stored_capital != self.settings.risk_capital_usdt
        if capital_rebased:
            historical_peak_gain = max(
                Decimal("0"), self.peak_equity - stored_capital
            )
            self.equity = (
                self.settings.risk_capital_usdt
                + self.cumulative_realized_pnl
                + self.unrealized_pnl
            )
            self.peak_equity = max(
                self.settings.risk_capital_usdt,
                self.equity,
                self.settings.risk_capital_usdt + historical_peak_gain,
            )
        self.realized_events = {
            str(key): {str(k): str(v) for k, v in dict(value).items()}
            for key, value in dict(state.get("realized_events") or {}).items()
        }
        now = datetime.now(timezone.utc)
        self.symbol_cooldown_until = {
            symbol: until for symbol, until in self.symbol_cooldown_until.items()
            if until > now
        }
        self._recompute_account(now=now)
        if recovered_reservation or capital_rebased:
            self._persist_state()

    @contextmanager
    def symbol_lock(self, symbol: Symbol) -> Iterator[None]:
        with self._symbol_locks[symbol]:
            yield

    def block_reasons(
        self, symbol: Symbol, notional: Decimal, *, risk_usdt: Decimal | None = None,
        side: Any | None = None, btc_beta: Decimal | None = None,
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
        if side is not None and btc_beta is not None:
            direction = Decimal("1") if str(getattr(side, "value", side)).upper() in {"LONG", "BUY"} else Decimal("-1")
            projected_beta_notional = sum(
                row.notional_usdt
                * (Decimal("1") if str(getattr(row.side, "value", row.side)).upper() in {"LONG", "BUY"} else Decimal("-1"))
                * (row.btc_beta if row.btc_beta is not None else Decimal("1"))
                for row in active
            ) + notional * direction * btc_beta
            if abs(projected_beta_notional) > self.settings.max_total_notional_usdt:
                reasons.append("maximum directional BTC-beta exposure reached")
        cooldown = self.symbol_cooldown_until.get(symbol)
        if cooldown and cooldown > current:
            reasons.append("symbol cooldown is active")
        if self.last_entry_at and current - self.last_entry_at < timedelta(seconds=self.settings.v2_global_entry_cooldown_seconds):
            reasons.append("global entry cooldown is active")
        recent = [
            item for item in self.reservations
            if (item.state in self.ACTIVE or item.execution_id is not None)
            and current - item.created_at <= timedelta(minutes=5)
        ]
        if len(recent) >= self.settings.max_new_entries_per_5_minutes:
            reasons.append("five-minute entry rate limit reached")
        today = [
            item for item in self.reservations
            if (item.state in self.ACTIVE or item.execution_id is not None)
            and item.created_at.date() == current.date()
        ]
        if len(today) >= self.settings.max_trades_per_day:
            reasons.append("daily trade cap reached")
        return reasons

    def reserve(
        self, *, run_id: str, candidate_id: Any, symbol: Symbol,
        strategy_name: StrategyName, notional: Decimal, risk_usdt: Decimal,
        side: Any | None = None, btc_beta: Decimal | None = None,
    ) -> PortfolioReservation | None:
        with self._global_lock, self.symbol_lock(symbol):
            existing = next(
                (
                    item for item in self.reservations
                    if str(item.candidate_id) == str(candidate_id)
                ),
                None,
            )
            if existing is not None:
                return existing if existing.state in self.ACTIVE else None
            if self.block_reasons(
                symbol, notional, risk_usdt=risk_usdt, side=side, btc_beta=btc_beta
            ):
                return None
            reservation = PortfolioReservation(
                run_id=run_id, candidate_id=candidate_id, symbol=symbol,
                strategy_name=strategy_name, correlation_group=correlation_group(symbol),
                notional_usdt=notional, risk_usdt=risk_usdt,
                side=side, btc_beta=btc_beta,
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
        item = self._transition(
            reservation_id, ReservationState.OPEN, execution_id=execution_id
        )
        if item is not None:
            self.last_entry_at = datetime.now(timezone.utc)
            self._persist_state()

    def release(
        self,
        reservation_id: Any,
        *,
        closed_at: datetime | None = None,
        activate_cooldown: bool = True,
    ) -> bool:
        current = closed_at or datetime.now(timezone.utc)
        with self._global_lock:
            item = next(
                (
                    row for row in self.reservations
                    if str(row.id) == str(reservation_id)
                ),
                None,
            )
            if item is None or item.state == ReservationState.RELEASED:
                return False
            item.state = ReservationState.RELEASED
            item.released_at = current
            if activate_cooldown and item.execution_id is not None:
                self.symbol_cooldown_until[item.symbol] = current + timedelta(
                    seconds=self.settings.v2_symbol_cooldown_seconds
                )
            updater = getattr(
                self.repository, "update_v2_portfolio_reservation", None
            )
            if callable(updater):
                updater(item)
            if not activate_cooldown and item.execution_id is None:
                remaining_entries = [
                    row.created_at for row in self.reservations
                    if row.state in self.ACTIVE or row.execution_id is not None
                ]
                self.last_entry_at = (
                    max(remaining_entries) if remaining_entries else None
                )
            self._persist_state()
            return True

    def apply_realized_pnl(self, pnl: Decimal, peak_equity: Decimal, equity: Decimal) -> None:
        """Backward-compatible manual ledger update used by older callers."""
        self.daily_pnl += pnl
        self.weekly_pnl += pnl
        self.cumulative_realized_pnl += pnl
        self.equity = equity
        self.peak_equity = max(self.peak_equity, peak_equity, equity)
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

    def apply_execution_result(self, record: DemoExecutionRecord) -> bool:
        """Credit one terminal execution exactly once to the durable ledger."""
        execution_id = str(record.id)
        if execution_id in self.realized_events:
            return False
        if record.realized_exchange_pnl is None or record.closed_at is None:
            return False
        self.realized_events[execution_id] = {
            "pnl": str(record.realized_exchange_pnl),
            "closed_at": record.closed_at.isoformat(),
            "fees": str(record.exchange_fees),
            "symbol": record.symbol.value,
        }
        self._recompute_account(now=datetime.now(timezone.utc))
        self._persist_state()
        return True

    def mark_to_market(
        self, records: list[DemoExecutionRecord], prices: dict[Symbol, Decimal],
        *, now: datetime | None = None,
    ) -> None:
        unrealized = Decimal("0")
        for record in records:
            if (
                record.state.value != "DEMO_POSITION_OPEN"
                or record.average_fill_price is None
                or record.accepted_quantity <= 0
                or record.symbol not in prices
            ):
                continue
            direction = Decimal("1") if record.side == Side.BUY else Decimal("-1")
            unrealized += (
                prices[record.symbol] - record.average_fill_price
            ) * record.accepted_quantity * direction
        self.unrealized_pnl = unrealized
        self._recompute_account(now=now or datetime.now(timezone.utc))
        self._persist_state()

    def _recompute_account(self, *, now: datetime) -> None:
        events: list[tuple[datetime, Decimal]] = []
        for value in self.realized_events.values():
            try:
                events.append((datetime.fromisoformat(value["closed_at"]), Decimal(value["pnl"])))
            except (KeyError, ValueError):
                continue
        self.cumulative_realized_pnl = sum((pnl for _, pnl in events), Decimal("0"))
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        self.daily_pnl = sum((pnl for stamp, pnl in events if stamp >= day_start), Decimal("0"))
        self.weekly_pnl = sum((pnl for stamp, pnl in events if stamp >= week_start), Decimal("0"))
        self.equity = (
            self.settings.risk_capital_usdt
            + self.cumulative_realized_pnl
            + self.unrealized_pnl
        )
        self.peak_equity = max(self.peak_equity, self.equity, self.settings.risk_capital_usdt)
        self.current_drawdown_pct = (
            (self.peak_equity - self.equity) / self.peak_equity * Decimal("100")
            if self.peak_equity > 0 else Decimal("0")
        )
        reasons: list[str] = []
        capital = self.settings.risk_capital_usdt
        if self.daily_pnl <= -(capital * self.settings.v2_max_daily_loss_pct / 100):
            reasons.append("maximum daily net loss reached")
        if self.weekly_pnl <= -(capital * self.settings.v2_max_weekly_loss_pct / 100):
            reasons.append("maximum weekly net loss reached")
        if self.current_drawdown_pct >= self.settings.v2_max_drawdown_pct:
            reasons.append("maximum portfolio drawdown reached")
        if reasons:
            self.kill_switch_active = True
            self.kill_switch_reasons.extend(
                reason for reason in reasons if reason not in self.kill_switch_reasons
            )

    def _transition(
        self, reservation_id: Any, state: ReservationState, *, execution_id: Any = None,
    ) -> PortfolioReservation | None:
        with self._global_lock:
            item = next((row for row in self.reservations if str(row.id) == str(reservation_id)), None)
            if item is None:
                return None
            if item.state == state and (
                execution_id is None or str(item.execution_id) == str(execution_id)
            ):
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
                "cumulative_realized_pnl": str(self.cumulative_realized_pnl),
                "unrealized_pnl": str(self.unrealized_pnl),
                "risk_capital_usdt": str(self.settings.risk_capital_usdt),
                "equity": str(self.equity),
                "peak_equity": str(self.peak_equity),
                "realized_events": self.realized_events,
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


def normalize_sized_order_quantity(
    target_notional: Decimal,
    minimum_position_notional: Decimal,
    price: Decimal,
    instrument: UniverseInstrument,
    hard_max_notional: Decimal,
) -> Decimal:
    """Normalize a sizing target without violating hard economic caps."""

    quantity = normalize_order_quantity(
        target_notional, price, instrument, hard_max_notional
    )
    if quantity * price >= minimum_position_notional:
        return quantity
    minimum_quantity = (
        minimum_position_notional / price / instrument.qty_step
    ).to_integral_value(rounding=ROUND_UP) * instrument.qty_step
    minimum_exchange_quantity = (
        instrument.min_notional_value / price / instrument.qty_step
    ).to_integral_value(rounding=ROUND_UP) * instrument.qty_step
    quantity = max(
        minimum_quantity,
        minimum_exchange_quantity,
        instrument.min_order_qty,
    )
    if quantity * price > hard_max_notional:
        raise ValueError("minimum normalized position exceeds a hard sizing cap")
    return quantity
