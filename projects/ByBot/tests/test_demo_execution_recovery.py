from datetime import datetime, timezone
from decimal import Decimal

from app.bybit.demo import _is_owned_bybit_protection_order
from app.bybit.demo_diagnostics import DemoDiagnosticsConfig
from app.bybit.demo_execution_recovery import (
    apply_demo_execution_repair,
    diagnose_demo_execution,
    exact_close_reconciliation_blockers,
)
from app.models import (
    DemoExecutionRecord,
    DemoExecutionState,
    DemoFill,
    ExecutionEnvironment,
    Side,
    Symbol,
)
from tests.test_bybit_demo_execution import candidate_bundle


class Repo:
    available = True

    def __init__(self, record, others=None):
        self.record = record
        self.others = list(others or [])
        self.repaired = None

    def load_demo_executions(self):
        return [self.record, *self.others]

    def load_demo_execution_events(self, execution_id):
        return []

    def repair_demo_execution(self, record, *, event_types, repair_payload):
        self.repaired = (record, event_types, repair_payload)
        return True


class ReadClient:
    def __init__(
        self, *, close=True, close_kind="manual", position_size="0", orders=None
    ):
        self.close = close
        self.close_kind = close_kind
        self.position_size = position_size
        self.orders = list(orders or [])

    def verify(self): pass

    def get_order_history(self, symbol):
        rows = [{
            "symbol": symbol.value,
            "orderId": "entry", "orderLinkId": "entry-link",
            "orderStatus": "Filled", "side": "Buy", "qty": "0.001",
            "cumExecQty": "0.001", "avgPrice": "100", "reduceOnly": False,
            "createdTime": "1000", "updatedTime": "1000",
        }]
        if self.close:
            metadata = {
                "manual": {"createType": "CreateByClosing"},
                "take_profit": {
                    "stopOrderType": "TakeProfit",
                    "createType": "CreateByTakeProfit",
                },
                "stop_loss": {
                    "stopOrderType": "StopLoss",
                    "createType": "CreateByStopLoss",
                },
            }[self.close_kind]
            rows.append({
                "symbol": symbol.value,
                "orderId": "external-close", "orderLinkId": "",
                "orderStatus": "Filled", "side": "Sell", "qty": "0.001",
                "cumExecQty": "0.001", "avgPrice": "110",
                "reduceOnly": True, "closeOnTrigger": True,
                "createdTime": "2000", "updatedTime": "2000",
                **metadata,
            })
        return rows

    def get_executions(self, symbol):
        rows = [{
            "symbol": symbol.value,
            "execId": "entry-fill", "orderId": "entry",
            "orderLinkId": "entry-link", "side": "Buy",
            "execQty": "0.001", "execPrice": "100", "execFee": "0.01",
            "execTime": "1000",
        }]
        if self.close:
            rows.append({
                "symbol": symbol.value,
                "execId": "close-fill", "orderId": "external-close",
                "orderLinkId": "", "side": "Sell", "execQty": "0.001",
                "execPrice": "110", "execFee": "0.02", "execTime": "2000",
            })
        return rows

    def get_positions(self, symbol):
        return [{"symbol": symbol.value, "size": self.position_size, "side": ""}]

    def get_open_orders(self): return list(self.orders)

    def get_closed_pnl(self, symbol):
        return ([{
            "orderId": "external-close", "closedSize": "0.001",
            "closedPnl": "-0.02", "avgEntryPrice": "100",
            "avgExitPrice": "110", "openFee": "0.01", "closeFee": "0.02",
        }] if self.close else [])

    def get_transaction_log(self): return []


def record():
    candidate, _, _, _ = candidate_bundle()
    return DemoExecutionRecord(
        candidate_id=candidate.id, run_id="run", order_link_id="entry-link",
        order_id="entry", state=DemoExecutionState.DEMO_POSITION_OPEN,
        symbol=Symbol.BTCUSDT, side=Side.BUY,
        requested_quantity=Decimal("0.001"), accepted_quantity=Decimal("0.001"),
        average_fill_price=Decimal("100"), take_profit=Decimal("110"),
        stop_loss=Decimal("90"), protection_confirmed=True,
    )


def config():
    return DemoDiagnosticsConfig("sqlite://", "key", "secret")


def test_remote_flat_with_attributable_external_close() -> None:
    item = record()
    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=Repo(item), client=ReadClient()
    )
    assert diagnosis.proposed_state == DemoExecutionState.DEMO_CLOSED_EXTERNALLY
    assert diagnosis.close_source == "manual_external_close"
    assert diagnosis.net_realized_pnl == Decimal("-0.02")
    assert exact_close_reconciliation_blockers(diagnosis) == []


def test_take_profit_can_terminalize_directly_from_position_open() -> None:
    item = record()
    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=Repo(item),
        client=ReadClient(close_kind="take_profit"),
    )
    assert diagnosis.proposed_state == DemoExecutionState.DEMO_CLOSED
    assert diagnosis.close_source == "take_profit"
    assert exact_close_reconciliation_blockers(diagnosis) == []


def test_stop_loss_can_terminalize_directly_from_position_open() -> None:
    item = record()
    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=Repo(item),
        client=ReadClient(close_kind="stop_loss"),
    )
    assert diagnosis.proposed_state == DemoExecutionState.DEMO_CLOSED
    assert diagnosis.close_source == "stop_loss"
    assert exact_close_reconciliation_blockers(diagnosis) == []


def test_remote_flat_without_attributable_close_has_no_fabricated_values() -> None:
    item, repo = record(), None
    repo = Repo(item)
    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=repo, client=ReadClient(close=False)
    )
    assert diagnosis.proposed_state == DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED
    assert apply_demo_execution_repair(diagnosis, repo)
    repaired, events, _ = repo.repaired
    assert repaired.close_order_id is None
    assert repaired.average_close_price is None
    assert repaired.realized_exchange_pnl is None
    assert repaired.close_fills == []
    assert events == [
        "REMOTE_POSITION_FLAT_VERIFIED", "CLOSE_ATTRIBUTION_UNAVAILABLE",
        "EXECUTION_FINALIZED_FLAT_VERIFIED",
    ]


def test_bybit_generated_protection_is_owned_only_on_full_match() -> None:
    item = record()
    position = [{
        "symbol": "BTCUSDT", "size": "0.001", "side": "Buy",
        "takeProfit": "110", "stopLoss": "90",
    }]
    protection = {
        "symbol": "BTCUSDT", "side": "Sell", "qty": "0.001",
        "reduceOnly": True, "closeOnTrigger": True,
        "stopOrderType": "TakeProfit", "triggerPrice": "110",
        "createType": "CreateByTakeProfit", "positionIdx": 0,
        "triggerDirection": 1, "orderLinkId": "",
    }
    assert _is_owned_bybit_protection_order(protection, item, position)
    assert not _is_owned_bybit_protection_order(
        {**protection, "closeOnTrigger": False}, item, position
    )


def test_remote_exposure_blocks_flat_repair() -> None:
    item = record()
    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=Repo(item),
        client=ReadClient(close=False, position_size="0.001"),
    )
    assert diagnosis.proposed_state is None
    assert "remote position is not flat" in diagnosis.blockers


def test_historical_close_before_entry_is_rejected_and_real_close_is_used() -> None:
    item = record().model_copy(update={
        "close_order_id": "historical-close",
        "close_fills": [DemoFill(
            execution_id="historical-exec", order_id="historical-close",
            quantity=Decimal("0.001"), price=Decimal("90"), fee=Decimal("0.01"),
            executed_at=datetime.fromtimestamp(0.5, tz=timezone.utc),
        )],
    })

    class TemporalClient(ReadClient):
        def get_order_history(self, symbol):
            rows = super().get_order_history(symbol)
            rows.append({
                "symbol": symbol.value,
                "orderId": "historical-close", "orderLinkId": "",
                "orderStatus": "Filled", "side": "Sell", "qty": "0.001",
                "cumExecQty": "0.001", "avgPrice": "90", "reduceOnly": True,
                "createdTime": "500", "updatedTime": "500",
            })
            return rows

        def get_executions(self, symbol):
            rows = super().get_executions(symbol)
            rows.append({
                "symbol": symbol.value,
                "execId": "historical-exec", "orderId": "historical-close",
                "side": "Sell", "execQty": "0.001", "execPrice": "90",
                "execFee": "0.01", "execTime": "500",
            })
            return rows

    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=Repo(item), client=TemporalClient()
    )
    assert diagnosis.rejected_close_order_ids == ["historical-close"]
    assert diagnosis.rejected_close_execution_ids == ["historical-exec"]
    assert diagnosis.close_executions[0]["execId"] == "close-fill"
    assert diagnosis.proposed_state == DemoExecutionState.DEMO_CLOSED_EXTERNALLY


def test_exact_persisted_close_reconciles_to_demo_closed() -> None:
    item = record().model_copy(update={
        "state": DemoExecutionState.DEMO_CLOSING,
        "close_order_id": "external-close",
        "close_reason": "exchange_generated_sl",
    })
    repo = Repo(item)
    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=repo,
        client=ReadClient(close_kind="stop_loss")
    )

    assert diagnosis.proposed_state == DemoExecutionState.DEMO_CLOSED
    assert exact_close_reconciliation_blockers(diagnosis) == []
    assert apply_demo_execution_repair(diagnosis, repo)
    repaired, events, _ = repo.repaired
    assert repaired.state == DemoExecutionState.DEMO_CLOSED
    assert repaired.failure_reason is None
    assert repaired.close_reason == "stop_loss"
    assert repaired.exit_attribution == "stop_loss"
    assert repaired.closed_at == datetime.fromtimestamp(2, tz=timezone.utc)
    assert events == [
        "READ_ONLY_RECONCILIATION_COMPLETED",
        "EXACT_CLOSE_ATTRIBUTED",
        "FINAL_REMOTE_STATE_FLAT",
        "PNL_ACCOUNTING_RECONCILED",
    ]


def test_exact_close_reconciliation_blocks_non_demo_execution() -> None:
    item = record().model_copy(update={
        "state": DemoExecutionState.DEMO_CLOSING,
        "close_order_id": "external-close",
        "close_reason": "exchange_generated_sl",
        "execution_environment": ExecutionEnvironment.PAPER,
    })
    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=Repo(item), client=ReadClient()
    )
    assert "execution does not belong to BYBIT_DEMO" in (
        exact_close_reconciliation_blockers(diagnosis)
    )


def test_exact_close_reconciliation_blocks_owned_open_order() -> None:
    item = record().model_copy(update={
        "state": DemoExecutionState.DEMO_CLOSING,
        "close_order_id": "external-close",
        "close_reason": "exchange_generated_sl",
    })
    open_close = {
        "symbol": "BTCUSDT", "orderId": "external-close",
        "orderLinkId": "", "side": "Sell", "qty": "0.001",
    }
    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=Repo(item),
        client=ReadClient(orders=[open_close]),
    )
    assert "bot-owned open order remains for execution" in (
        exact_close_reconciliation_blockers(diagnosis)
    )


def test_exact_close_reconciliation_blocks_global_fill_identity_conflict() -> None:
    item = record().model_copy(update={
        "state": DemoExecutionState.DEMO_CLOSING,
        "close_order_id": "external-close",
        "close_reason": "exchange_generated_sl",
        "close_fills": [DemoFill(
            execution_id="close-fill", order_id="external-close",
            quantity=Decimal("0.001"), price=Decimal("110"),
            executed_at=datetime.fromtimestamp(2, tz=timezone.utc),
        )],
    })
    other = record().model_copy(update={
        "close_order_id": "external-close",
        "close_fills": [DemoFill(
            execution_id="close-fill", order_id="external-close",
            quantity=Decimal("0.001"), price=Decimal("110"),
            executed_at=datetime.fromtimestamp(2, tz=timezone.utc),
        )],
    })
    diagnosis = diagnose_demo_execution(
        config(), str(item.id), repository=Repo(item, [other]), client=ReadClient()
    )
    blockers = exact_close_reconciliation_blockers(diagnosis)
    assert "exchange order ID is assigned to another Demo execution" in blockers
    assert "exchange execution ID is assigned to another Demo execution" in blockers
