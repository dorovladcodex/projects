from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.v2.certification_monitor import (
    CertificationMonitorHealth,
    CertificationMonitorState,
    ExecutionFallbackEvidence,
    ProtectionEstablishmentState,
    StatusFallbackEvidence,
    classify_status_fallback,
)
from scripts.demo_v2_certification_monitor import (
    collect_status_fallback_evidence,
    runtime_blockers,
)
from scripts.demo_v2_protection_pending_canary import decimal_duration_ms


NOW = datetime(2026, 7, 28, 23, 38, 8, tzinfo=timezone.utc)


def test_canary_duration_uses_decimal_arithmetic() -> None:
    start = datetime.fromisoformat("2026-07-30T11:08:59.708000+00:00")
    end = datetime.fromisoformat("2026-07-30T11:09:13.440053+00:00")
    assert decimal_duration_ms(start, end) == Decimal("13732.053")


def execution(
    execution_id: str = "36f3abbd-e7e1-41ab-b5df-ecd0ee9a7a5c",
    *,
    symbol: str = "SUIUSDT",
    state: str = "DEMO_CLOSING",
    open_position: bool = False,
    protected: bool = False,
    remote_flat: bool = True,
    exact_close: bool = True,
    pending: bool = False,
    residuals: int = 0,
    partial: bool = False,
    conflict: bool = False,
    error: str | None = None,
    entry_owned: bool = False,
    protection_state: ProtectionEstablishmentState | None = None,
    remaining_ms: float | None = None,
    elapsed_ms: float | None = None,
    fill_at: str | None = None,
    invalid_reason: str | None = None,
) -> ExecutionFallbackEvidence:
    return ExecutionFallbackEvidence(
        execution_id=execution_id,
        symbol=symbol,
        durable_state=state,
        remote_position_open=open_position,
        protection_confirmed=protected,
        remote_flat=remote_flat,
        exact_close_evidence=exact_close,
        close_evidence_pending=pending,
        exact_owned_residual_orders=residuals,
        partial_close=partial,
        ownership_conflict=conflict,
        evidence_error=error,
        entry_owned=entry_owned,
        protection_state=protection_state,
        protection_remaining_deadline_ms=remaining_ms,
        protection_elapsed_ms=elapsed_ms,
        fill_at=fill_at,
        invalid_protection_reason=invalid_reason,
    )


def evidence(
    *executions: ExecutionFallbackEvidence,
    runner: bool = True,
    uvicorn: bool = True,
    listener: bool = True,
    persistence: bool = True,
    kill_switch: bool = False,
    unrelated_positions: int = 0,
    unrelated_orders: int = 0,
    conflicts: int = 0,
    complete: bool = True,
) -> StatusFallbackEvidence:
    return StatusFallbackEvidence(
        runner_alive=runner,
        uvicorn_alive=uvicorn,
        port_listening=listener,
        persistence_ok=persistence,
        kill_switch_active=kill_switch,
        executions=tuple(executions),
        unrelated_positions=unrelated_positions,
        unrelated_orders=unrelated_orders,
        ownership_conflicts=conflicts,
        authoritative_check_complete=complete,
    )


def record(
    monitor: CertificationMonitorHealth,
    fallback: StatusFallbackEvidence,
    now: datetime = NOW,
):
    return monitor.record_status_failure(
        now=now,
        evidence=fallback,
        error="TimeoutError: /v2/status timed out",
    )


def test_safe_degradation_is_not_a_monitor_blocker() -> None:
    v2 = {
        "safety_critical_failures": 0,
        "data_integrity_failures": 0,
        "unexpected_cycle_failures": 0,
        "total_cycle_failures": 0,
        "safe_degraded_events": 4,
        "persistence_status": "OK",
        "kill_switch_active": False,
    }
    demo = {
        "kill_switch_active": False,
        "confirmed_unrelated_orders": 0,
        "ownership_conflicts": 0,
    }
    assert runtime_blockers(v2, demo, []) == []


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("safety_critical_failures", "safety-critical failure"),
        ("data_integrity_failures", "data-integrity-critical failure"),
        ("unexpected_cycle_failures", "unexpected cycle failure"),
    ],
)
def test_explicit_critical_counters_remain_monitor_blockers(
    field: str, message: str
) -> None:
    v2 = {
        "safety_critical_failures": 0,
        "data_integrity_failures": 0,
        "unexpected_cycle_failures": 0,
        "persistence_status": "OK",
        "kill_switch_active": False,
    }
    v2[field] = 1
    blockers = runtime_blockers(
        v2,
        {
            "kill_switch_active": False,
            "confirmed_unrelated_orders": 0,
            "ownership_conflicts": 0,
        },
        [],
    )
    assert message in blockers


def test_status_timeout_without_exposure_is_degraded_not_failed() -> None:
    decision = classify_status_fallback(evidence())
    assert decision.state == CertificationMonitorState.STATUS_DEGRADED
    assert not decision.escalate
    assert decision.keep_reconciler_alive


def test_open_rest_confirmed_protected_position_keeps_runtime_alive() -> None:
    item = execution(
        open_position=True, protected=True, remote_flat=False, exact_close=False
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.PROTECTED_POSITION_DEGRADED
    assert not decision.escalate


def test_open_unprotected_position_fails_fast() -> None:
    item = execution(
        open_position=True, protected=False, remote_flat=False, exact_close=False
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.FAIL_FAST
    assert decision.escalate and decision.shutdown_ready


def pending_execution(
    *,
    state: ProtectionEstablishmentState = (
        ProtectionEstablishmentState.PROTECTION_PENDING
    ),
    remaining_ms: float = 29_976,
    elapsed_ms: float = 24,
) -> ExecutionFallbackEvidence:
    return execution(
        state="DEMO_ORDER_ACKNOWLEDGED",
        open_position=True,
        protected=False,
        remote_flat=False,
        exact_close=False,
        entry_owned=True,
        protection_state=state,
        remaining_ms=remaining_ms,
        elapsed_ms=elapsed_ms,
        fill_at="2026-07-29T13:53:23.895000+00:00",
    )


def test_immediate_post_fill_poll_is_protection_pending() -> None:
    decision = classify_status_fallback(evidence(pending_execution()))
    assert decision.state == CertificationMonitorState.PROTECTION_PENDING
    assert not decision.escalate
    assert decision.keep_reconciler_alive


def test_entry_acknowledged_inside_deadline_is_not_fail_fast() -> None:
    item = pending_execution(
        state=ProtectionEstablishmentState.ENTRY_ACKNOWLEDGED
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.PROTECTION_PENDING


def test_partial_fill_inside_deadline_is_not_fail_fast() -> None:
    item = pending_execution(
        state=ProtectionEstablishmentState.ENTRY_PARTIALLY_FILLED
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.PROTECTION_PENDING


def test_protection_deadline_expiry_fails_fast() -> None:
    decision = classify_status_fallback(
        evidence(pending_execution(remaining_ms=0, elapsed_ms=30_000))
    )
    assert decision.state == CertificationMonitorState.FAIL_FAST
    assert "deadline expired" in str(decision.reason)


def test_invalid_authoritative_protection_fails_immediately() -> None:
    item = pending_execution(
        state=ProtectionEstablishmentState.PROTECTION_INVALIDATED_BY_MARKET
    )
    item = ExecutionFallbackEvidence(
        **{
            **item.__dict__,
            "invalid_protection_reason": "stop loss differs",
        }
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.FAIL_FAST
    assert "invalid or contradictory" in str(decision.reason)


def test_incomplete_exact_entry_evidence_is_bounded_ambiguity() -> None:
    item = execution(
        open_position=True,
        protected=False,
        remote_flat=False,
        exact_close=False,
        protection_state=ProtectionEstablishmentState.SAFETY_AMBIGUOUS,
        error="authoritative entry evidence fetch pending",
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.SAFETY_AMBIGUOUS
    assert not decision.escalate
    assert decision.keep_reconciler_alive


def test_repeated_protection_pending_polls_are_one_incident() -> None:
    monitor = CertificationMonitorHealth(protection_timeout_seconds=30)
    fallback = evidence(pending_execution())
    record(monitor, fallback)
    record(monitor, fallback, NOW + timedelta(seconds=2))
    record(monitor, fallback, NOW + timedelta(seconds=4))
    assert monitor.incident_count == 1
    assert monitor.protection_pending_count == 1
    assert [event["event"] for event in monitor.events].count(
        "MONITOR_PROTECTION_PENDING"
    ) == 1


def test_link_incident_replay_is_pending_then_protected() -> None:
    import json

    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "link_protection_establishment_20260729.json"
        ).read_text(encoding="utf-8")
    )
    pending = pending_execution(
        remaining_ms=(
            fixture["protection_deadline_ms"]
            - fixture["monitor_elapsed_ms"]
        ),
        elapsed_ms=fixture["monitor_elapsed_ms"],
    )
    first = classify_status_fallback(evidence(pending))
    assert first.state == CertificationMonitorState.PROTECTION_PENDING

    protected = execution(
        open_position=True,
        protected=True,
        remote_flat=False,
        exact_close=False,
        entry_owned=True,
        protection_state=ProtectionEstablishmentState.PROTECTED,
    )
    final = classify_status_fallback(evidence(protected))
    assert final.state == CertificationMonitorState.PROTECTED_POSITION_DEGRADED
    assert fixture["protection_confirmation_elapsed_ms"] < 30_000


def test_natural_close_during_protection_window_uses_terminalization() -> None:
    item = execution(
        open_position=False,
        protected=False,
        remote_flat=True,
        exact_close=True,
        entry_owned=True,
        protection_state=ProtectionEstablishmentState.TERMINALIZATION_PENDING,
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING


def test_persistence_failure_during_pending_fails_immediately() -> None:
    decision = classify_status_fallback(
        evidence(pending_execution(), persistence=False)
    )
    assert decision.state == CertificationMonitorState.FAIL_FAST


def test_unrelated_order_during_pending_fails_immediately() -> None:
    decision = classify_status_fallback(
        evidence(pending_execution(), unrelated_orders=1)
    )
    assert decision.state == CertificationMonitorState.FAIL_FAST


class FakeProtectionRepository:
    def __init__(self, record: SimpleNamespace) -> None:
        self.record = record

    def load_demo_executions(self):
        return [self.record]

    def load_demo_execution_events(self, execution_id: str):
        assert execution_id == str(self.record.id)
        return []


class FakeProtectionReadClient:
    def __init__(
        self,
        *,
        take_profit: str = "",
        stop_loss: str = "",
        include_history: bool = True,
        include_executions: bool = True,
        include_realtime: bool = True,
        realtime_overrides: dict | None = None,
    ) -> None:
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.include_history = include_history
        self.include_executions = include_executions
        self.include_realtime = include_realtime
        self.realtime_overrides = dict(realtime_overrides or {})
        self.mutations = 0

    def verify(self) -> None:
        return None

    def get_usdt_positions(self):
        return [{
            "symbol": "LINKUSDT",
            "side": "SELL",
            "size": "18",
            "takeProfit": self.take_profit,
            "stopLoss": self.stop_loss,
        }]

    def get_open_orders(self):
        return []

    def get_realtime_order(
        self, symbol, *, order_id=None, order_link_id=None,
    ):
        assert symbol.value == "LINKUSDT"
        assert order_id or order_link_id
        if not self.include_realtime:
            return []
        row = {
            "category": "linear",
            "orderId": "entry-order",
            "orderLinkId": "entry-link",
            "symbol": "LINKUSDT",
            "side": "SELL",
            "positionIdx": 0,
            "reduceOnly": False,
            "closeOnTrigger": False,
            "qty": "18",
            "cumExecQty": "18",
            "orderStatus": "Filled",
            "createdTime": "1785333203800",
            "updatedTime": "1785333203895",
        }
        row.update(self.realtime_overrides)
        return [row]

    def get_order_history(self, symbol):
        if not self.include_history:
            return []
        return [{
            "orderId": "entry-order",
            "orderLinkId": "entry-link",
            "symbol": "LINKUSDT",
            "side": "SELL",
            "orderStatus": "Filled",
            "cumExecQty": "18",
            "updatedTime": "1785333203895",
        }]

    def get_executions(self, symbol):
        if not self.include_executions:
            return []
        return [{
            "execId": "entry-fill",
            "orderId": "entry-order",
            "orderLinkId": "entry-link",
            "symbol": "LINKUSDT",
            "side": "SELL",
            "execQty": "18",
            "execTime": "1785333203895",
        }]


def protection_record(*, order_id: str | None = "entry-order") -> SimpleNamespace:
    return SimpleNamespace(
        id="execution-link",
        symbol=SimpleNamespace(value="LINKUSDT"),
        side=SimpleNamespace(value="SELL"),
        state=SimpleNamespace(value="DEMO_ORDER_ACKNOWLEDGED"),
        order_id=order_id,
        order_link_id="entry-link",
        close_order_id=None,
        close_order_link_id=None,
        tp_order_id=None,
        sl_order_id=None,
        fills=[],
        close_fills=[],
        requested_quantity=Decimal("18"),
        accepted_quantity=Decimal("0"),
        take_profit=Decimal("8.288"),
        stop_loss=Decimal("8.337"),
        protection_confirmed_at=None,
        exchange_fill_at=None,
        protection_position_idx=0,
        exchange_submit_started_at=datetime.fromisoformat(
            "2026-07-29T13:53:23.800000+00:00"
        ),
        order_submitted_at=datetime.fromisoformat(
            "2026-07-29T13:53:23.800000+00:00"
        ),
        order_acknowledged_at=datetime.fromisoformat(
            "2026-07-29T13:53:23.850000+00:00"
        ),
    )


def collect_link_evidence(
    client: FakeProtectionReadClient,
    *,
    observed_at: datetime,
    order_id: str | None = "entry-order",
):
    record_value = protection_record(order_id=order_id)
    return collect_status_fallback_evidence(
        config=SimpleNamespace(),
        repository=FakeProtectionRepository(record_value),
        client=client,
        active=[record_value],
        runner_alive=True,
        uvicorn_alive=True,
        listener=True,
        persistence_ok=True,
        kill_switch_active=False,
        observed_at=observed_at,
        protection_timeout_seconds=30,
    )


def test_authoritative_collector_classifies_link_at_24ms_as_pending() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(), observed_at=observed
    )
    item = fallback.executions[0]
    assert item.entry_owned is True
    assert item.ownership_conflict is False
    assert item.protection_state == (
        ProtectionEstablishmentState.ENTRY_ACKNOWLEDGED
    )
    assert 24 <= item.protection_elapsed_ms < 25
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.PROTECTION_PENDING
    )


def test_exact_order_link_owns_fill_before_order_id_is_persisted() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(),
        observed_at=observed,
        order_id=None,
    )
    item = fallback.executions[0]
    assert item.entry_owned is True
    assert item.ownership_conflict is False
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.PROTECTION_PENDING
    )


def test_filled_order_history_bridges_delayed_execution_history() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(include_executions=False),
        observed_at=observed,
        order_id=None,
    )
    item = fallback.executions[0]
    assert item.entry_owned is True
    assert item.fill_at == "2026-07-29T13:53:23.895000+00:00"
    assert item.protection_state == (
        ProtectionEstablishmentState.ENTRY_ACKNOWLEDGED
    )
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.PROTECTION_PENDING
    )


def test_authoritative_rest_protection_confirmation_wins_over_cache() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:38.949264+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(
            take_profit="8.288",
            stop_loss="8.337",
        ),
        observed_at=observed,
    )
    item = fallback.executions[0]
    assert item.protection_state == ProtectionEstablishmentState.PROTECTED
    assert item.protection_confirmed is True
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.PROTECTED_POSITION_DEGRADED
    )


def test_invalid_rest_protection_fails_without_waiting_deadline() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:30+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(
            take_profit="8.200",
            stop_loss="8.337",
        ),
        observed_at=observed,
    )
    assert fallback.executions[0].protection_state == (
        ProtectionEstablishmentState.PROTECTION_INVALIDATED_BY_MARKET
    )
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.FAIL_FAST
    )


def test_late_entry_history_is_bounded_exact_attribution_pending() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(
            include_history=False,
            include_executions=False,
            include_realtime=False,
        ),
        observed_at=observed,
    )
    item = fallback.executions[0]
    assert item.ownership_conflict is False
    assert item.protection_state == (
        ProtectionEstablishmentState.EXACT_ENTRY_ATTRIBUTION_PENDING
    )
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.EXACT_ENTRY_ATTRIBUTION_PENDING
    )


def test_realtime_order_bridges_position_before_history() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(
            include_history=False,
            include_executions=False,
        ),
        observed_at=observed,
    )
    item = fallback.executions[0]
    assert item.entry_owned is True
    assert item.entry_attribution_source == "realtime_order"
    assert item.realtime_order_id == "entry-order"
    assert item.realtime_identity_match is True
    assert item.protection_state == (
        ProtectionEstablishmentState.ENTRY_ACKNOWLEDGED
    )
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.PROTECTION_PENDING
    )


def test_realtime_order_link_bridges_before_order_id_is_durable() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(
            include_history=False,
            include_executions=False,
        ),
        observed_at=observed,
        order_id=None,
    )
    item = fallback.executions[0]
    assert item.entry_owned is True
    assert item.realtime_order_link_id == "entry-link"
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.PROTECTION_PENDING
    )


def test_realtime_partial_fill_matches_visible_position() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    client = FakeProtectionReadClient(
        include_history=False,
        include_executions=False,
        realtime_overrides={
            "cumExecQty": "9",
            "orderStatus": "PartiallyFilled",
        },
    )
    client.get_usdt_positions = lambda: [{
        "symbol": "LINKUSDT",
        "side": "SELL",
        "positionIdx": 0,
        "size": "9",
        "takeProfit": "",
        "stopLoss": "",
    }]
    item = collect_link_evidence(
        client, observed_at=observed
    ).executions[0]
    assert item.entry_owned is True
    assert item.protection_state == (
        ProtectionEstablishmentState.ENTRY_PARTIALLY_FILLED
    )


def test_realtime_identity_mismatch_fails_closed() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(
            include_history=False,
            include_executions=False,
            realtime_overrides={"orderId": "different-order"},
        ),
        observed_at=observed,
    )
    item = fallback.executions[0]
    assert item.entry_owned is False
    assert item.ownership_conflict is True
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.FAIL_FAST
    )


def test_reduce_only_realtime_order_cannot_prove_entry() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(
            include_history=False,
            include_executions=False,
            realtime_overrides={"reduceOnly": True},
        ),
        observed_at=observed,
    )
    assert fallback.executions[0].ownership_conflict is True
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.FAIL_FAST
    )


def test_zero_cumulative_realtime_quantity_cannot_prove_entry() -> None:
    observed = datetime.fromisoformat("2026-07-29T13:53:23.919476+00:00")
    fallback = collect_link_evidence(
        FakeProtectionReadClient(
            include_history=False,
            include_executions=False,
            realtime_overrides={"cumExecQty": "0"},
        ),
        observed_at=observed,
    )
    assert fallback.executions[0].ownership_conflict is True
    assert classify_status_fallback(fallback).state == (
        CertificationMonitorState.FAIL_FAST
    )


def test_three_real_canary_replays_stay_inside_protection_window() -> None:
    cases = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "protection_pending_canaries_20260730.json"
        ).read_text(encoding="utf-8")
    )
    assert len(cases) == 3
    for case in cases:
        latency_ms = float(case["protection_latency_ms"])
        assert 7_000 <= latency_ms <= 17_000
        item = ExecutionFallbackEvidence(
            execution_id=case["execution_id"],
            symbol=case["symbol"],
            durable_state="DEMO_ORDER_ACKNOWLEDGED",
            remote_position_open=True,
            protection_confirmed=False,
            remote_flat=False,
            entry_owned=True,
            protection_state=ProtectionEstablishmentState.PROTECTION_PENDING,
            fill_at=case["fill_at"],
            protection_elapsed_ms=latency_ms,
            protection_remaining_deadline_ms=30_000 - latency_ms,
            entry_attribution_source="realtime_order",
            realtime_order_id=case["order_id"],
            realtime_order_link_id=case["order_link_id"],
            realtime_order_quantity=case["quantity"],
            realtime_cumulative_quantity=case["quantity"],
            realtime_identity_match=True,
        )
        assert classify_status_fallback(evidence(item)).state == (
            CertificationMonitorState.PROTECTION_PENDING
        )


def test_natural_close_with_durable_closing_is_terminalization_pending() -> None:
    decision = classify_status_fallback(evidence(execution()))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING
    assert decision.request_reconciliation
    assert decision.keep_reconciler_alive
    assert not decision.shutdown_ready


def test_two_simultaneous_incident_closes_are_terminalization_pending() -> None:
    sui = execution()
    eth = execution(
        "c0eb0cd0-9d0f-4a2d-ae70-63c97dd37455",
        symbol="ETHUSDT",
    )
    decision = classify_status_fallback(evidence(sui, eth))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING
    assert decision.request_reconciliation


def test_close_evidence_arriving_during_bound_stays_pending() -> None:
    monitor = CertificationMonitorHealth(terminalization_timeout_seconds=120)
    pending = execution(exact_close=False, pending=True)
    assert record(monitor, evidence(pending)).state == (
        CertificationMonitorState.TERMINALIZATION_PENDING
    )
    exact = execution(exact_close=True, pending=False)
    decision = record(monitor, evidence(exact), NOW + timedelta(seconds=19))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING
    assert not decision.escalate


def test_remote_flat_without_exact_or_pending_evidence_is_ambiguous() -> None:
    item = execution(
        exact_close=False,
        error="entry order and fill attribution disagree",
    )
    decision = classify_status_fallback(evidence(item))
    assert decision.state == CertificationMonitorState.SAFETY_AMBIGUOUS
    assert not decision.escalate


def test_partial_close_evidence_is_ambiguous() -> None:
    decision = classify_status_fallback(
        evidence(execution(exact_close=False, partial=True))
    )
    assert decision.state == CertificationMonitorState.SAFETY_AMBIGUOUS


def test_conflicting_close_identity_fails_fast() -> None:
    decision = classify_status_fallback(
        evidence(execution(exact_close=False, conflict=True))
    )
    assert decision.state == CertificationMonitorState.FAIL_FAST


def test_exact_owned_residual_orders_remain_terminalization_pending() -> None:
    decision = classify_status_fallback(evidence(execution(residuals=2)))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING


def test_unrelated_external_order_fails_immediately() -> None:
    decision = classify_status_fallback(
        evidence(execution(), unrelated_orders=1)
    )
    assert decision.state == CertificationMonitorState.FAIL_FAST


def test_authoritative_rest_evidence_overrides_stale_cached_counts() -> None:
    # Cached /v2/status counts are intentionally absent from the classifier.
    decision = classify_status_fallback(evidence(execution()))
    assert decision.state == CertificationMonitorState.TERMINALIZATION_PENDING


def test_supervisor_does_not_terminate_reconciler_while_pending() -> None:
    monitor = CertificationMonitorHealth()
    decision = record(monitor, evidence(execution()))
    assert not decision.escalate
    assert decision.keep_reconciler_alive
    assert not decision.shutdown_ready


def test_repeated_pending_polls_are_one_deduplicated_incident() -> None:
    monitor = CertificationMonitorHealth()
    fallback = evidence(execution())
    record(monitor, fallback)
    record(monitor, fallback, NOW + timedelta(seconds=5))
    record(monitor, fallback, NOW + timedelta(seconds=10))
    assert monitor.incident_count == 1
    assert monitor.terminalization_pending_count == 1
    assert [event["event"] for event in monitor.events].count(
        "MONITOR_TERMINALIZATION_PENDING"
    ) == 1


def test_terminalization_bound_gets_one_ambiguity_window_before_fail_fast() -> None:
    monitor = CertificationMonitorHealth(terminalization_timeout_seconds=10)
    fallback = evidence(execution())
    record(monitor, fallback)
    boundary = record(monitor, fallback, NOW + timedelta(seconds=10))
    assert boundary.state == CertificationMonitorState.SAFETY_AMBIGUOUS
    assert not boundary.escalate
    final = record(monitor, fallback, NOW + timedelta(seconds=20))
    assert final.state == CertificationMonitorState.FAIL_FAST
    assert final.escalate


def test_status_recovery_returns_to_healthy_without_phase_mutation() -> None:
    monitor = CertificationMonitorHealth()
    record(monitor, evidence(execution()))
    decision = monitor.record_status_success(now=NOW + timedelta(seconds=20))
    assert decision.state == CertificationMonitorState.HEALTHY
    assert monitor.recovered_count == 1


def test_resolved_durable_terminalization_window_is_observed_once() -> None:
    monitor = CertificationMonitorHealth()

    monitor.record_resolved_terminalization_evidence(
        now=NOW,
        execution_id="execution-1",
        started_at="2026-07-29T11:59:58+00:00",
        completed_at="2026-07-29T11:59:59+00:00",
    )
    monitor.record_resolved_terminalization_evidence(
        now=NOW,
        execution_id="execution-1",
        started_at="2026-07-29T11:59:58+00:00",
        completed_at="2026-07-29T11:59:59+00:00",
    )

    assert monitor.terminalization_pending_count == 1
    events = [
        item for item in monitor.events
        if item["event"] == "MONITOR_TERMINALIZATION_PENDING"
    ]
    assert len(events) == 1
    assert events[0]["resolved"] is True
    assert events[0]["observed_via"] == "durable_terminalization_timestamps"


def test_process_persistence_kill_switch_and_unrelated_state_fail_fast() -> None:
    cases = (
        evidence(runner=False),
        evidence(uvicorn=False),
        evidence(listener=False),
        evidence(persistence=False),
        evidence(kill_switch=True),
        evidence(unrelated_positions=1),
        evidence(conflicts=1),
    )
    for fallback in cases:
        assert classify_status_fallback(fallback).state == (
            CertificationMonitorState.FAIL_FAST
        )


def test_monitor_has_no_exchange_mutation_surface() -> None:
    forbidden = {
        "create_order",
        "cancel_order",
        "set_trading_stop",
        "set_leverage",
        "close_position",
    }
    assert forbidden.isdisjoint(dir(CertificationMonitorHealth()))


def test_two_close_canary_requires_explicit_demo_order_authorization() -> None:
    source = (
        Path(__file__).parents[1]
        / "scripts"
        / "demo_v2_two_close_canary.py"
    ).read_text(encoding="utf-8")
    assert 'parser.add_argument("--allow-demo-orders", action="store_true")' in source
    assert '"BYBIT_LIVE_TRADING_ENABLED": "false"' in source
    assert '"BYBIT_ENABLE_TRADING": "false"' in source
    assert '"BYBIT_PRIVATE_DEMO_BASE_URL": "https://api-demo.bybit.com"' in source
    assert "if not args.allow_demo_orders:" in source


def test_protection_pending_canary_is_guarded_and_uses_production_paths() -> None:
    source = (
        Path(__file__).parents[1]
        / "scripts"
        / "demo_v2_protection_pending_canary.py"
    ).read_text(encoding="utf-8")
    assert 'parser.add_argument("--allow-demo-orders", action="store_true")' in source
    assert "if not args.allow_demo_orders:" in source
    assert 'f"/v2/canary/sizing/{symbol}/{args.notional_usdt}"' in source
    assert 'f"/demo/canary/{execution_id}/close"' in source
    assert '"--protection-timeout-seconds"' in source
    assert '"30"' in source
