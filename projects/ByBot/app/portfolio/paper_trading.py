from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import RLock

from app.models import (
    MarketSnapshot,
    PaperPnl,
    PaperPosition,
    PositionStatus,
    RiskDecision,
    NewsSignalAction,
    NewsSignalCandidate,
    SignalRiskPreview,
    Side,
    Symbol,
    TradeSignal,
)


class PaperTradingService:
    """In-memory paper trading loop. It never calls Bybit order endpoints."""

    def __init__(
        self,
        *,
        timeout: timedelta = timedelta(minutes=60),
        starting_equity: float = 10_000.0,
        repository: object | None = None,
        max_total_open_positions: int = 1,
        symbol_cooldown: timedelta = timedelta(0),
        global_entry_cooldown: timedelta = timedelta(0),
        max_daily_net_loss_pct: float = 2.0,
        max_weekly_net_loss_pct: float = 5.0,
        max_account_drawdown_pct: float = 10.0,
    ) -> None:
        self.timeout = timeout
        self.starting_equity = starting_equity
        self.repository = repository
        self.max_total_open_positions = max_total_open_positions
        self.symbol_cooldown = symbol_cooldown
        self.global_entry_cooldown = global_entry_cooldown
        self.max_daily_net_loss_pct = max_daily_net_loss_pct
        self.max_weekly_net_loss_pct = max_weekly_net_loss_pct
        self.max_account_drawdown_pct = max_account_drawdown_pct
        self.positions: list[PaperPosition] = []
        self.closed_trades: list[PaperPosition] = []
        self.last_risk_decision: RiskDecision | None = None
        self.last_error: str | None = None
        self.paper_execution_attempts = 0
        self.paper_positions_opened = 0
        self.paper_positions_closed = 0
        self.paper_execution_duplicates_blocked = 0
        self.paper_execution_risk_blocked = 0
        self.executed_candidate_ids: set[str] = set()
        self._execution_lock = RLock()
        self.last_execution_attempted = False
        self.last_position_opened = False
        self.last_execution_duplicate = False
        self.last_execution_details: dict[str, object] | None = None
        self.last_existing_execution_state: str | None = None
        self.last_execution_error_code: str | None = None
        self.last_execution_retryable = False
        self.kill_switch_active = False
        self.kill_switch_reasons: list[str] = []
        self.peak_equity = starting_equity
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        self.current_drawdown_pct = 0.0
        self.last_entry_at: datetime | None = None
        self.symbol_cooldown_until: dict[Symbol, datetime] = {}

    @property
    def equity(self) -> float:
        return self.starting_equity + self.realized_pnl + self.unrealized_pnl

    @property
    def realized_pnl(self) -> float:
        return sum(position.realized_pnl for position in self.closed_trades)

    @property
    def unrealized_pnl(self) -> float:
        return sum(
            position.unrealized_pnl
            for position in self.positions
            if position.status == PositionStatus.OPEN
        )

    @property
    def fees_paid(self) -> float:
        return sum(item.fees_paid for item in self.closed_trades) + sum(
            item.fees_paid
            for item in self.positions
            if item.status == PositionStatus.OPEN
        )

    @property
    def open_positions(self) -> list[PaperPosition]:
        return [
            position
            for position in self.positions
            if position.status == PositionStatus.OPEN
        ]

    def restore(self) -> None:
        recover = getattr(self.repository, "recover_orphaned_paper_executions", None)
        if callable(recover):
            recover()
        loader = getattr(self.repository, "load_paper_positions", None)
        if not callable(loader):
            return
        restored = loader()
        self.positions = list(restored)
        self.closed_trades = [
            position for position in restored if position.status == PositionStatus.CLOSED
        ]
        account_loader = getattr(self.repository, "load_or_create_paper_account", None)
        if callable(account_loader):
            account = account_loader(self.starting_equity)
            if account is not None:
                self.starting_equity = float(account["starting_equity"])
        risk_loader = getattr(self.repository, "load_or_create_paper_risk_state", None)
        if callable(risk_loader):
            state = risk_loader(self.starting_equity)
            if state is not None:
                self.kill_switch_active = bool(state["kill_switch_active"])
                self.kill_switch_reasons = list(state["kill_switch_reasons"])
                self.peak_equity = float(state["peak_equity"])
                self.daily_pnl = float(state["daily_pnl"])
                self.weekly_pnl = float(state["weekly_pnl"])
                self.current_drawdown_pct = float(state["current_drawdown_pct"])
                self.last_entry_at = _parse_datetime(state.get("last_entry_at"))
                self.symbol_cooldown_until = {
                    Symbol(symbol): parsed
                    for symbol, value in dict(state["symbol_cooldowns"]).items()
                    if (parsed := _parse_datetime(value)) is not None
                }
        self.paper_positions_opened = sum(
            position.candidate_id is not None for position in restored
        )
        self.paper_positions_closed = sum(
            position.candidate_id is not None
            and position.status == PositionStatus.CLOSED
            for position in restored
        )
        executed_loader = getattr(self.repository, "executed_candidate_ids", None)
        if callable(executed_loader):
            self.executed_candidate_ids = executed_loader()
        self.refresh_risk_controls()

    def refresh_risk_controls(
        self, *, now: datetime | None = None, persist: bool = True
    ) -> None:
        now = _as_utc(now or datetime.now(timezone.utc))
        self._expire_cooldowns(now)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        self.daily_pnl = sum(
            item.realized_pnl
            for item in self.closed_trades
            if item.closed_at is not None and _as_utc(item.closed_at) >= day_start
        )
        self.weekly_pnl = sum(
            item.realized_pnl
            for item in self.closed_trades
            if item.closed_at is not None and _as_utc(item.closed_at) >= week_start
        )
        self.peak_equity = max(self.peak_equity, self.equity, self.starting_equity)
        self.current_drawdown_pct = (
            max(0.0, (self.peak_equity - self.equity) / self.peak_equity * 100)
            if self.peak_equity > 0
            else 0.0
        )

        triggered: list[str] = []
        if self.daily_pnl <= -(
            self.starting_equity * self.max_daily_net_loss_pct / 100
        ):
            triggered.append("maximum daily net loss reached")
        if self.weekly_pnl <= -(
            self.starting_equity * self.max_weekly_net_loss_pct / 100
        ):
            triggered.append("maximum weekly net loss reached")
        if self.current_drawdown_pct >= self.max_account_drawdown_pct:
            triggered.append("maximum paper account drawdown reached")
        if triggered:
            self.kill_switch_active = True
            self.kill_switch_reasons = list(
                dict.fromkeys(self.kill_switch_reasons + triggered)
            )
        if persist:
            self._persist_risk_state(now)

    def reset_kill_switch(self, *, now: datetime | None = None) -> dict[str, object]:
        now = _as_utc(now or datetime.now(timezone.utc))
        self.kill_switch_active = False
        self.kill_switch_reasons = []
        self.peak_equity = max(self.equity, self.starting_equity)
        self.current_drawdown_pct = 0.0
        self._persist_risk_state(now)
        return self.risk_status(now=now, refresh=False)

    def entry_block_reasons(
        self, symbol: Symbol, *, now: datetime | None = None
    ) -> list[str]:
        now = _as_utc(now or datetime.now(timezone.utc))
        self.refresh_risk_controls(now=now)
        reasons: list[str] = []
        if self.kill_switch_active:
            reasons.extend(self.kill_switch_reasons or ["paper kill switch is active"])
        if any(item.symbol == symbol for item in self.open_positions):
            reasons.append("maximum one open paper position per symbol reached")
        if len(self.open_positions) >= self.max_total_open_positions:
            reasons.append(
                "maximum open paper positions reached: "
                "maximum total open paper positions reached"
            )
        global_until = self._global_cooldown_until()
        if global_until is not None and now < global_until:
            reasons.append("global paper entry cooldown is active")
        symbol_until = self.symbol_cooldown_until.get(symbol)
        if symbol_until is not None and now < symbol_until:
            reasons.append(f"paper symbol cooldown is active for {symbol.value}")
        return list(dict.fromkeys(reasons))

    def risk_status(
        self, *, now: datetime | None = None, refresh: bool = True
    ) -> dict[str, object]:
        now = _as_utc(now or datetime.now(timezone.utc))
        if refresh:
            self.refresh_risk_controls(now=now)
        global_until = self._global_cooldown_until()
        global_remaining = (
            max(0.0, (global_until - now).total_seconds())
            if global_until is not None else 0.0
        )
        symbol_state = {
            symbol.value: {
                "until": until.isoformat(),
                "remaining_seconds": max(0.0, (until - now).total_seconds()),
            }
            for symbol, until in self.symbol_cooldown_until.items()
            if until > now
        }
        general_entries_allowed = (
            not self.kill_switch_active
            and len(self.open_positions) < self.max_total_open_positions
            and global_remaining <= 0
        )
        return {
            "entries_allowed": general_entries_allowed,
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_reasons": list(self.kill_switch_reasons),
            "current_drawdown_pct": self.current_drawdown_pct,
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "cooldown_state": {
                "global_until": global_until.isoformat() if global_until else None,
                "global_remaining_seconds": global_remaining,
                "symbols": symbol_state,
            },
            "open_positions": len(self.open_positions),
            "maximum_positions": self.max_total_open_positions,
            "last_execution_error": self.last_execution_error_code or self.last_error,
        }

    def _global_cooldown_until(self) -> datetime | None:
        if self.last_entry_at is None or self.global_entry_cooldown <= timedelta(0):
            return None
        return self.last_entry_at + self.global_entry_cooldown

    def _expire_cooldowns(self, now: datetime) -> None:
        self.symbol_cooldown_until = {
            symbol: until
            for symbol, until in self.symbol_cooldown_until.items()
            if until > now
        }

    def _persist_risk_state(self, now: datetime) -> None:
        saver = getattr(self.repository, "save_paper_risk_state", None)
        if not callable(saver):
            return
        saver({
            "kill_switch_active": self.kill_switch_active,
            "kill_switch_reasons": list(self.kill_switch_reasons),
            "peak_equity": self.peak_equity,
            "daily_pnl": self.daily_pnl,
            "weekly_pnl": self.weekly_pnl,
            "current_drawdown_pct": self.current_drawdown_pct,
            "last_entry_at": self.last_entry_at,
            "symbol_cooldowns": {
                symbol.value: until.isoformat()
                for symbol, until in self.symbol_cooldown_until.items()
            },
            "updated_at": now,
        })

    def open_from_candidate(
        self,
        candidate: NewsSignalCandidate,
        risk_preview: SignalRiskPreview,
        market: MarketSnapshot,
        *,
        taker_fee_bps: float,
        slippage_bps: float,
    ) -> PaperPosition | None:
        with self._execution_lock:
            return self._open_from_candidate(
                candidate, risk_preview, market,
                taker_fee_bps=taker_fee_bps, slippage_bps=slippage_bps,
            )

    def _open_from_candidate(
        self,
        candidate: NewsSignalCandidate,
        risk_preview: SignalRiskPreview,
        market: MarketSnapshot,
        *,
        taker_fee_bps: float,
        slippage_bps: float,
    ) -> PaperPosition | None:
        self.paper_execution_attempts += 1
        self.last_execution_attempted = False
        self.last_position_opened = False
        self.last_execution_duplicate = False
        self.last_execution_details = None
        self.last_existing_execution_state = None
        self.last_execution_error_code = None
        self.last_execution_retryable = False
        candidate_key = str(candidate.id)
        if candidate_key in self.executed_candidate_ids:
            self.paper_execution_duplicates_blocked += 1
            self.last_error = "paper execution already exists for candidate"
            self.last_execution_duplicate = True
            existing = next(
                (item for item in self.positions if item.candidate_id == candidate.id), None
            )
            self.last_existing_execution_state = (
                "PAPER_CLOSED" if existing and existing.status == PositionStatus.CLOSED
                else "PAPER_OPENED" if existing else None
            )
            self.last_execution_details = (
                existing.model_dump(mode="json") if existing else None
            )
            return existing
        if not risk_preview.preview_performed or not risk_preview.approved:
            self.paper_execution_risk_blocked += 1
            self.last_error = "risk preview is not approved"
            return None
        entry_blocks = self.entry_block_reasons(market.symbol)
        if entry_blocks:
            self.paper_execution_risk_blocked += 1
            self.last_error = "; ".join(entry_blocks)
            return None
        execution_key = f"paper:{candidate_key}"
        side = Side.BUY if candidate.final_action == NewsSignalAction.BUY else Side.SELL
        entry_price = market.ask_price if side == Side.BUY else market.bid_price
        size = risk_preview.capped_size
        if size <= 0:
            self.paper_execution_risk_blocked += 1
            self.last_error = "approved paper size is not positive"
            self._update_execution(candidate_key, "EXECUTION_BLOCKED", None)
            return None
        notional = entry_price * size
        stop_distance = entry_price * candidate.proposed_stop_loss_pct / 100
        take_profit_distance = entry_price * candidate.proposed_take_profit_pct / 100
        entry_fee = notional * taker_fee_bps / 10_000
        entry_slippage = notional * slippage_bps / 10_000
        position = PaperPosition(
            symbol=market.symbol, side=side, size=size, entry_price=entry_price,
            current_price=market.last_price,
            stop_loss=entry_price - stop_distance if side == Side.BUY else entry_price + stop_distance,
            take_profit=entry_price + take_profit_distance if side == Side.BUY else entry_price - take_profit_distance,
            unrealized_pnl=0,
            estimated_entry_fee=entry_fee,
            estimated_exit_fee=entry_fee,
            estimated_entry_slippage=entry_slippage,
            estimated_exit_slippage=entry_slippage,
            reason="automatic_paper_execution",
            candidate_id=candidate.id,
            risk_decision_id=risk_preview.risk_decision_id,
            execution_key=execution_key,
            position_notional=notional,
            fees_paid=entry_fee,
            slippage_paid=entry_slippage,
        )
        position.unrealized_pnl = self._auto_net_pnl(position, market.last_price)
        atomic_persist = getattr(self.repository, "persist_paper_open_transaction", None)
        if callable(atomic_persist):
            transaction = atomic_persist(
                candidate_key,
                risk_preview,
                position,
                max_total_open_positions=self.max_total_open_positions,
                starting_equity=self.starting_equity,
            )
            if transaction.get("status") == "EXISTING":
                self.paper_execution_duplicates_blocked += 1
                self.last_execution_duplicate = True
                self.last_existing_execution_state = str(transaction.get("state") or "")
                existing = next(
                    (item for item in self.positions if item.candidate_id == candidate.id), None
                )
                self.last_execution_details = (
                    existing.model_dump(mode="json")
                    if existing else transaction.get("payload")
                )
                return existing
            if transaction.get("status") == "ERROR":
                self.last_error = "paper persistence transaction failed"
                self.last_execution_error_code = str(
                    transaction.get("error_code") or "DB_PERSISTENCE_ERROR"
                )
                self.last_execution_retryable = bool(transaction.get("retryable", True))
                return None
            if transaction.get("status") == "BLOCKED":
                self.paper_execution_risk_blocked += 1
                self.last_error = str(
                    transaction.get("reason") or "paper position limit reached"
                )
                return None
            position.risk_decision_id = int(transaction["risk_decision_id"])
            execution_key = str(transaction["execution_key"])
            position.execution_key = execution_key
        else:
            reserve = getattr(self.repository, "reserve_paper_execution", None)
            reservation = (
                reserve(candidate_key, risk_preview.risk_decision_id)
                if callable(reserve) else {
                    "status": "RESERVED",
                    "execution_id": str(candidate.id),
                    "execution_key": execution_key,
                }
            )
            if reservation is None:
                self.last_error = "paper execution reservation failed"
                return None
            execution_key = str(reservation["execution_key"])
            position.execution_key = execution_key
        self.last_execution_attempted = True
        self.positions.append(position)
        self.executed_candidate_ids.add(candidate_key)
        self.paper_positions_opened += 1
        self.last_position_opened = True
        self.last_execution_details = position.model_dump(mode="json")
        self.last_error = None
        self.last_entry_at = datetime.now(timezone.utc)
        self.refresh_risk_controls(now=self.last_entry_at)
        if not callable(atomic_persist):
            self._persist(position)
            self._update_execution(candidate_key, "PAPER_OPENED", position)
        return position

    def _update_execution(
        self, candidate_id: str, state: str, position: PaperPosition | None
    ) -> None:
        updater = getattr(self.repository, "update_paper_execution", None)
        if callable(updater):
            payload = (
                {
                    **position.model_dump(mode="json"),
                    "quantity": position.size,
                    "notional": position.position_notional,
                    "entry_fee": position.estimated_entry_fee,
                    "exit_fee": position.estimated_exit_fee,
                    "entry_slippage": position.estimated_entry_slippage,
                    "exit_slippage": position.estimated_exit_slippage,
                    "close_reason": position.close_reason,
                }
                if position else {"candidate_id": candidate_id}
            )
            updater(
                candidate_id, state, payload,
                position_id=str(position.id) if position else None,
            )

    def _auto_net_pnl(self, position: PaperPosition, price: float) -> float:
        gross = (
            (price - position.entry_price) * position.size
            if position.side == Side.BUY
            else (position.entry_price - price) * position.size
        )
        exit_notional = price * position.size
        fee_rate = (
            position.estimated_entry_fee / position.position_notional
            if position.position_notional else 0
        )
        slippage_rate = (
            position.estimated_entry_slippage / position.position_notional
            if position.position_notional else 0
        )
        return gross - position.estimated_entry_fee - exit_notional * fee_rate \
            - position.estimated_entry_slippage - exit_notional * slippage_rate

    def _persist(self, position: PaperPosition) -> None:
        saver = getattr(self.repository, "save_paper_position", None)
        if callable(saver):
            saver(position)

    @property
    def open_position(self) -> PaperPosition | None:
        return next(iter(self.open_positions), None)

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
        with self._execution_lock:
            return self._open_from_signal(
                signal, risk_decision, market, take_profit_pct=take_profit_pct
            )

    def _open_from_signal(
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
        entry_blocks = self.entry_block_reasons(signal.symbol)
        if entry_blocks:
            self.last_error = "; ".join(entry_blocks)
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
        size = risk_decision.capped_size or (risk_decision.max_loss_amount / stop_distance)
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
            unrealized_pnl=_calculate_pnl(
                signal.side,
                size,
                entry_price,
                market.last_price,
                estimated_fees=risk_decision.estimated_fees,
                estimated_slippage=risk_decision.estimated_slippage,
            ),
            estimated_entry_fee=risk_decision.estimated_fees / 2,
            estimated_exit_fee=risk_decision.estimated_fees / 2,
            estimated_entry_slippage=risk_decision.estimated_slippage / 2,
            estimated_exit_slippage=risk_decision.estimated_slippage / 2,
            reason="opened_by_test_signal",
        )
        self.positions.append(position)
        self._persist(position)
        self.last_entry_at = datetime.now(timezone.utc)
        self.refresh_risk_controls(now=self.last_entry_at)
        self.last_error = None
        return position

    def update_from_market(
        self,
        market: MarketSnapshot,
        *,
        now: datetime | None = None,
    ) -> PaperPosition | None:
        position = next(
            (item for item in self.open_positions if item.symbol == market.symbol),
            None,
        )
        if position is None:
            return None

        now = now or datetime.now(timezone.utc)
        position.current_price = market.last_price
        position.unrealized_pnl = (
            self._auto_net_pnl(position, market.last_price)
            if position.candidate_id else _calculate_pnl(
                position.side, position.size, position.entry_price, market.last_price,
                estimated_fees=position.estimated_entry_fee + position.estimated_exit_fee,
                estimated_slippage=position.estimated_entry_slippage + position.estimated_exit_slippage,
            )
        )

        if _stop_loss_hit(position):
            return self.close_position(
                market.last_price, reason="stop_loss", now=now,
                position_id=position.id,
            )
        if _take_profit_hit(position):
            return self.close_position(
                market.last_price, reason="take_profit", now=now,
                position_id=position.id,
            )
        if now - position.opened_at >= self.timeout:
            return self.close_position(
                market.last_price, reason="timeout", now=now,
                position_id=position.id,
            )
        self._persist(position)
        self.refresh_risk_controls(now=now)
        return position

    def close_position(
        self,
        exit_price: float,
        *,
        reason: str,
        now: datetime | None = None,
        position_id: object | None = None,
    ) -> PaperPosition:
        with self._execution_lock:
            return self._close_position(
                exit_price, reason=reason, now=now, position_id=position_id
            )

    def _close_position(
        self,
        exit_price: float,
        *,
        reason: str,
        now: datetime | None = None,
        position_id: object | None = None,
    ) -> PaperPosition:
        allowed_reasons = {"manual_close", "stop_loss", "take_profit", "timeout"}
        if reason not in allowed_reasons:
            self.last_error = f"unsupported close reason: {reason}"
            raise ValueError(self.last_error)
        open_position = next(
            (
                item for item in self.open_positions
                if position_id is None or str(item.id) == str(position_id)
            ),
            None,
        )
        if open_position is None:
            self.last_error = "no open paper position"
            raise RuntimeError(self.last_error)
        now = now or datetime.now(timezone.utc)
        position = open_position.model_copy(deep=True)
        position.current_price = exit_price
        if position.candidate_id:
            gross = (
                (exit_price - position.entry_price) * position.size
                if position.side == Side.BUY
                else (position.entry_price - exit_price) * position.size
            )
            exit_notional = exit_price * position.size
            fee_rate = position.estimated_entry_fee / position.position_notional
            slippage_rate = position.estimated_entry_slippage / position.position_notional
            position.estimated_exit_fee = exit_notional * fee_rate
            position.estimated_exit_slippage = exit_notional * slippage_rate
            position.gross_pnl = gross
            position.fees_paid = position.estimated_entry_fee + position.estimated_exit_fee
            position.slippage_paid = (
                position.estimated_entry_slippage + position.estimated_exit_slippage
            )
            position.realized_pnl = gross - position.fees_paid - position.slippage_paid
        else:
            gross = (
                (exit_price - position.entry_price) * position.size
                if position.side == Side.BUY
                else (position.entry_price - exit_price) * position.size
            )
            position.gross_pnl = gross
            position.fees_paid = (
                position.estimated_entry_fee + position.estimated_exit_fee
            )
            position.slippage_paid = (
                position.estimated_entry_slippage + position.estimated_exit_slippage
            )
            position.realized_pnl = (
                gross - position.fees_paid - position.slippage_paid
            )
        position.unrealized_pnl = 0.0
        position.status = PositionStatus.CLOSED
        position.closed_at = now
        position.reason = reason
        position.close_reason = reason

        atomic_close = getattr(self.repository, "persist_paper_close_transaction", None)
        if callable(atomic_close):
            transaction = atomic_close(position, self.starting_equity)
            if transaction.get("status") == "ERROR":
                self.last_error = "paper close persistence transaction failed"
                raise RuntimeError(self.last_error)
            if transaction.get("status") == "EXISTING":
                position = PaperPosition.model_validate(transaction["position"])

        for index, item in enumerate(self.positions):
            if item.id == position.id:
                self.positions[index] = position
                break
        if not any(item.id == position.id for item in self.closed_trades):
            self.closed_trades.append(position)
            self.paper_positions_closed += 1
        if not callable(atomic_close):
            self._persist(position)
            if position.candidate_id:
                self._update_execution(
                    str(position.candidate_id), "PAPER_CLOSED", position
                )
        if self.symbol_cooldown > timedelta(0):
            self.symbol_cooldown_until[position.symbol] = (
                _as_utc(now) + self.symbol_cooldown
            )
        self.refresh_risk_controls(now=_as_utc(now))
        self.last_error = None
        return position

    def pnl(self) -> PaperPnl:
        return PaperPnl(
            starting_equity=self.starting_equity,
            equity=self.equity,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            total_pnl=self.realized_pnl + self.unrealized_pnl,
            fees_paid=self.fees_paid,
            open_positions=sum(
                item.status == PositionStatus.OPEN for item in self.positions
            ),
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
        risk = self.risk_status()
        return {
            "status": self.status,
            "starting_equity": self.starting_equity,
            "equity": self.equity,
            "open_position": (
                self.open_position.model_dump(mode="json") if self.open_position else None
            ),
            "open_positions": self.positions_payload(),
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
            "paper_execution_attempts": self.paper_execution_attempts,
            "paper_positions_opened": self.paper_positions_opened,
            "paper_positions_closed": self.paper_positions_closed,
            "paper_execution_duplicates_blocked": self.paper_execution_duplicates_blocked,
            "paper_execution_risk_blocked": self.paper_execution_risk_blocked,
            "paper_fees_paid": self.fees_paid,
            **risk,
        }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str) and value:
        try:
            return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _calculate_pnl(
    side: Side,
    size: float,
    entry_price: float,
    current_price: float,
    *,
    estimated_fees: float = 0.0,
    estimated_slippage: float = 0.0,
) -> float:
    if side == Side.BUY:
        gross_pnl = (current_price - entry_price) * size
    else:
        gross_pnl = (entry_price - current_price) * size
    return gross_pnl - estimated_fees - estimated_slippage


def _stop_loss_hit(position: PaperPosition) -> bool:
    if position.side == Side.BUY:
        return position.current_price <= position.stop_loss
    return position.current_price >= position.stop_loss


def _take_profit_hit(position: PaperPosition) -> bool:
    if position.side == Side.BUY:
        return position.current_price >= position.take_profit
    return position.current_price <= position.take_profit
