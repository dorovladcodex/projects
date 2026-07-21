from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.bybit.demo import DemoExecutionService, TERMINAL_DEMO_STATES
from app.config import Settings
from app.models import DemoExecutionRecord, DemoExecutionState, Side, Symbol
from app.v2.models import ReservationState, StrategyName
from app.v2.portfolio import PortfolioRiskService
from app.v2.runtime import V2Runtime


@dataclass(frozen=True)
class DemoReplayFixture:
    execution_id: UUID
    run_id: str
    symbol: Symbol
    side: Side
    entry_order_id: str
    order_link_id: str
    close_order_id: str
    entry_execution_id: str
    close_execution_id: str
    quantity: Decimal
    entry_price: Decimal
    close_price: Decimal
    entry_fee: Decimal
    close_fee: Decimal
    close_source: str
    sparse_entry_fill_order_id: bool = False

    @classmethod
    def load(cls, path: Path) -> "DemoReplayFixture":
        value = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            execution_id=UUID(value["execution_id"]), run_id=value["run_id"],
            symbol=Symbol(value["symbol"]), side=Side(value["side"]),
            entry_order_id=value["entry_order_id"],
            order_link_id=value["order_link_id"],
            close_order_id=value["close_order_id"],
            entry_execution_id=value["entry_execution_id"],
            close_execution_id=value["close_execution_id"],
            quantity=Decimal(value["quantity"]),
            entry_price=Decimal(value["entry_price"]),
            close_price=Decimal(value["close_price"]),
            entry_fee=Decimal(value["entry_fee"]),
            close_fee=Decimal(value["close_fee"]),
            close_source=value["close_source"],
            sparse_entry_fill_order_id=bool(
                value.get("sparse_entry_fill_order_id", False)
            ),
        )


@dataclass
class DemoReplayResult:
    record: DemoExecutionRecord
    lifecycle_events: list[str]
    completed_trades: int
    open_count: int
    reservation_release_count: int
    cooldown_update_count: int
    unresolved_execution_ids: list[str]
    terminal_event_count: int
    exchange_mutation_attempts: int


class ReplayRepository:
    """Thread-safe deterministic store implementing the real service contract."""

    def __init__(self, record: DemoExecutionRecord) -> None:
        self.record = record.model_copy(deep=True)
        self.events: list[dict[str, Any]] = []
        self.private_event_keys: set[str] = set()
        self.released_reservation_ids: set[str] = set()
        self.cooldown_symbols: set[str] = set()
        self._lock = RLock()

    def load_demo_kill_switch(self):
        return None

    def get_demo_execution(self, candidate_id: str):
        with self._lock:
            if str(self.record.candidate_id) != candidate_id:
                return None
            return self.record.model_copy(deep=True)

    def load_demo_executions(self):
        with self._lock:
            return [self.record.model_copy(deep=True)]

    def find_demo_execution(self, order_link_id: str, order_id: str):
        with self._lock:
            if (
                order_link_id
                and order_link_id in {
                    self.record.order_link_id, self.record.close_order_link_id,
                }
            ) or (
                order_id
                and order_id in {self.record.order_id, self.record.close_order_id}
            ):
                return self.record.model_copy(deep=True)
            return None

    def record_demo_event(self, key: str, event_type: str, payload: dict[str, Any]):
        with self._lock:
            if key in self.private_event_keys:
                return False
            self.private_event_keys.add(key)
            return True

    def save_demo_execution(self, record: DemoExecutionRecord, *, event_type: str):
        with self._lock:
            if self.record.state in TERMINAL_DEMO_STATES:
                return True
            record.order_id = record.order_id or self.record.order_id
            record.order_link_id = record.order_link_id or self.record.order_link_id
            record.close_order_id = record.close_order_id or self.record.close_order_id
            record.close_order_link_id = (
                record.close_order_link_id or self.record.close_order_link_id
            )
            self.record = record.model_copy(deep=True)
            self.events.append({
                "event_type": event_type,
                "state": record.state.value,
                "occurred_at": record.updated_at.isoformat(),
            })
            return True

    def terminalize_demo_execution(
        self, record: DemoExecutionRecord, *, event_type: str,
    ) -> str:
        with self._lock:
            if self.record.state in TERMINAL_DEMO_STATES:
                return "ALREADY_TERMINAL"
            self.record = record.model_copy(deep=True)
            self.events.append({
                "event_type": event_type,
                "state": record.state.value,
                "occurred_at": record.updated_at.isoformat(),
            })
            return "APPLIED"

    def load_demo_execution_events(self, execution_id: str):
        return list(self.events) if execution_id == str(self.record.id) else []

    def update_v2_portfolio_reservation(self, reservation):
        if reservation.state == ReservationState.RELEASED:
            self.released_reservation_ids.add(str(reservation.id))
        return True

    def save_v2_portfolio_state(self, payload):
        self.cooldown_symbols.update((payload.get("symbol_cooldowns") or {}).keys())
        return True


class ReplayBybitClient:
    """Read-only exchange snapshot; every mutation method is a hard failure."""

    def __init__(self, fixture: DemoReplayFixture, *, opened_at: datetime) -> None:
        self.fixture = fixture
        self.mutation_attempts = 0
        close_side = "Sell" if fixture.side == Side.BUY else "Buy"
        entry_side = "Buy" if fixture.side == Side.BUY else "Sell"
        entry_ms = int(opened_at.timestamp() * 1000)
        close_ms = entry_ms + 60_000
        stop_type = "TakeProfit" if fixture.close_source == "take_profit" else "StopLoss"
        self.history = [
            {
                "symbol": fixture.symbol.value,
                "orderId": fixture.entry_order_id,
                "orderLinkId": fixture.order_link_id,
                "orderStatus": "Filled", "side": entry_side,
                "qty": str(fixture.quantity),
                "cumExecQty": str(fixture.quantity),
                "avgPrice": str(fixture.entry_price),
                "reduceOnly": False,
                "createdTime": str(entry_ms), "updatedTime": str(entry_ms),
            },
            {
                "symbol": fixture.symbol.value,
                "orderId": fixture.close_order_id, "orderLinkId": "",
                "orderStatus": "Filled", "side": close_side,
                "qty": str(fixture.quantity),
                "cumExecQty": str(fixture.quantity),
                "avgPrice": str(fixture.close_price),
                "reduceOnly": True, "closeOnTrigger": True,
                "stopOrderType": stop_type,
                "createType": f"CreateBy{stop_type}",
                "createdTime": str(close_ms), "updatedTime": str(close_ms),
            },
        ]
        self.executions = [
            {
                "symbol": fixture.symbol.value,
                "orderId": fixture.entry_order_id,
                "orderLinkId": fixture.order_link_id,
                "execId": fixture.entry_execution_id,
                "side": entry_side, "execQty": str(fixture.quantity),
                "execPrice": str(fixture.entry_price),
                "execFee": str(fixture.entry_fee), "execTime": str(entry_ms),
            },
            {
                "symbol": fixture.symbol.value,
                "orderId": fixture.close_order_id, "orderLinkId": "",
                "execId": fixture.close_execution_id,
                "side": close_side, "execQty": str(fixture.quantity),
                "execPrice": str(fixture.close_price),
                "execFee": str(fixture.close_fee), "execTime": str(close_ms),
                "stopOrderType": stop_type,
                "createType": f"CreateBy{stop_type}",
            },
        ]
        self.positions = [{
            "symbol": fixture.symbol.value, "size": str(fixture.quantity),
            "side": entry_side, "positionIdx": 0,
            "takeProfit": "1", "stopLoss": "1",
            "updatedTime": str(close_ms + 1_000),
        }]
        self.open_orders: list[dict[str, Any]] = []

    def get_open_orders(self, symbol=None, settle_coin=None):
        return list(self.open_orders)

    def get_order_history(self, symbol=None, settle_coin=None):
        return list(self.history)

    def get_executions(self, symbol=None, settle_coin=None):
        return list(self.executions)

    def get_positions(self, symbol=None, settle_coin=None):
        return list(self.positions)

    def get_closed_pnl(self, symbol=None, settle_coin=None):
        return []

    def _mutation(self, *_args, **_kwargs):
        self.mutation_attempts += 1
        raise AssertionError("exchange mutation is forbidden in replay")

    create_order = _mutation
    cancel_order = _mutation
    set_trading_stop = _mutation
    set_leverage = _mutation


class DemoV2ReplayHarness:
    def __init__(self, fixture: DemoReplayFixture) -> None:
        self.fixture = fixture

    def run(self) -> DemoReplayResult:
        opened_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        candidate_id = uuid5(NAMESPACE_URL, f"replay:{self.fixture.execution_id}")
        record = DemoExecutionRecord(
            id=self.fixture.execution_id, candidate_id=candidate_id,
            run_id=self.fixture.run_id, order_link_id=self.fixture.order_link_id,
            order_id=self.fixture.entry_order_id,
            state=DemoExecutionState.DEMO_ORDER_ACKNOWLEDGED,
            symbol=self.fixture.symbol, side=self.fixture.side,
            requested_quantity=self.fixture.quantity,
            accepted_quantity=Decimal("0"),
            protection_confirmed=True,
            created_at=opened_at - timedelta(seconds=1),
            updated_at=opened_at - timedelta(seconds=1),
        )
        repository = ReplayRepository(record)
        client = ReplayBybitClient(self.fixture, opened_at=opened_at)
        settings = Settings(
            _env_file=None, app_env="demo", test_mode=False,
            bot_mode="BYBIT_DEMO", execution_mode="BYBIT_DEMO",
            bybit_env="demo", bybit_api_key="replay-key",
            bybit_api_secret="replay-secret",
            bybit_demo_trading_enabled=True,
            demo_order_execution_authorized=True,
            bybit_enable_trading=False, bybit_live_trading_enabled=False,
            v2_enabled=True, v2_auto_demo_execution=False,
        )
        service = DemoExecutionService(
            settings, repository, client, run_id=self.fixture.run_id
        )
        portfolio = PortfolioRiskService(settings, repository)
        reservation = portfolio.reserve(
            run_id=self.fixture.run_id, candidate_id=candidate_id,
            symbol=self.fixture.symbol,
            strategy_name=StrategyName.VOLUME_BREAKOUT,
            notional=Decimal("50"), risk_usdt=Decimal("1"),
        )
        if reservation is None:
            raise AssertionError("replay portfolio reservation was rejected")
        portfolio.mark_open(reservation.id, self.fixture.execution_id)
        lifecycle = ["entry acknowledged"]

        entry = dict(client.executions[0])
        if self.fixture.sparse_entry_fill_order_id:
            entry["orderId"] = None
        service.handle_private_event({"topic": "execution", "data": [entry]})
        lifecycle.append("entry fully filled")
        current = repository.get_demo_execution(str(candidate_id))
        current.state = DemoExecutionState.DEMO_POSITION_OPEN
        current.protection_confirmed = True
        current.updated_at = opened_at + timedelta(seconds=10)
        repository.save_demo_execution(current, event_type="DEMO_POSITION_OPEN")
        lifecycle.append("position open")

        service.handle_private_event({
            "topic": "execution", "data": [client.executions[1]],
        })
        lifecycle.append("TP execution fill" if self.fixture.close_source == "take_profit" else "SL execution fill")
        service.handle_private_event({
            "topic": "order", "data": [client.history[1]],
        })
        lifecycle.append("CLOSE_Filled")
        client.positions = [{
            "symbol": self.fixture.symbol.value, "size": "0", "side": "",
            "positionIdx": 0, "takeProfit": "", "stopLoss": "",
            "updatedTime": client.history[1]["updatedTime"],
        }]
        service.handle_private_event({
            "topic": "position", "data": [client.positions[0]],
        })
        lifecycle += ["POSITION_FLAT_PENDING_PNL", "remote flat", "no remaining orders"]
        final = service._reconcile_execution_rest(
            repository.get_demo_execution(str(candidate_id))
        )
        runtime_view = SimpleNamespace(repository=repository, portfolio=portfolio)
        V2Runtime._sync_reservations(runtime_view)
        V2Runtime._sync_reservations(runtime_view)

        terminal_count = sum(
            item["event_type"] == "DEMO_CLOSE_TERMINALIZED"
            for item in repository.events
        )
        terminal = final.state in TERMINAL_DEMO_STATES
        return DemoReplayResult(
            record=final, lifecycle_events=lifecycle,
            completed_trades=int(terminal), open_count=0 if terminal else 1,
            reservation_release_count=len(repository.released_reservation_ids),
            cooldown_update_count=len(repository.cooldown_symbols),
            unresolved_execution_ids=[] if terminal else [str(final.id)],
            terminal_event_count=terminal_count,
            exchange_mutation_attempts=client.mutation_attempts,
        )
