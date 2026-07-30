from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bybit.demo import DemoSafetyError
from app.models import DemoExecutionState, ProtectionDataState
from tests.test_bybit_demo_execution import (
    ProtectionReplayClient,
    _run_trailing_update,
    _trailing_record,
    _trailing_service,
    _wif_rules,
    demo_settings,
    MemoryRepository,
)
from app.bybit.demo import DemoExecutionService
from app.v2.runtime import protection_management_price_input


FIXTURE = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "v2_cycle_311_protection_freshness.json"
    ).read_text(encoding="utf-8")
)


def _stale_update(
    service: DemoExecutionService,
    record,
    *,
    now: datetime | None = None,
    data_fresh: bool = True,
):
    observed = now or datetime.now(timezone.utc)
    return service.monitor_strategy_position(
        str(record.id),
        Decimal("0.15570"),
        market_price_at=observed - timedelta(seconds=16),
        market_price_received_at=observed,
        market_price_source="v2_feature_last_trade",
        data_fresh=data_fresh,
        now=observed,
    )


def test_cycle_311_fixture_preserves_exact_incident_identity() -> None:
    assert FIXTURE["execution_id"] == "43b5b657-a95d-4571-8a94-237f5f5382ff"
    assert FIXTURE["symbol"] == "ADAUSDT"
    assert FIXTURE["exchange_mutation_attempted"] is False


def test_protected_position_with_fresh_management_data_updates() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    result = _run_trailing_update(service, record)
    assert result.protection_data_state == ProtectionDataState.PROTECTION_DATA_FRESH
    assert len(client.set_stop_calls) == 1


def test_protected_position_with_stale_data_defers_update() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    result = _stale_update(service, record)
    assert result.protection_data_state == (
        ProtectionDataState.PROTECTION_UPDATE_DEFERRED_STALE_PRICE
    )


def test_first_stale_management_sample_does_not_trigger_stale_signal_exit() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, repo, record = _trailing_service(client)
    service.settings.v2_position_data_stale_exit_seconds = 15
    result = _stale_update(service, record)
    assert result.state == DemoExecutionState.DEMO_POSITION_OPEN
    assert result.protection_data_state == (
        ProtectionDataState.PROTECTION_UPDATE_DEFERRED_STALE_PRICE
    )
    assert client.orders == []
    assert not any(event == "CLOSE_SUBMITTING" for event, _ in repo.saved_events)


def test_existing_tp_sl_remain_rest_confirmed_during_deferral() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    result = _stale_update(service, record)
    assert result.last_protection_verification["source"] == "REST"
    assert result.last_protection_verification["classification"] == (
        "PROTECTED_WITH_STALE_MANAGEMENT_DATA"
    )
    assert result.protection_data_evidence[
        "existing_exchange_protection_remained_valid"
    ] is True


def test_fresh_data_later_arrives_and_update_proceeds() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, repo, record = _trailing_service(client)
    stale = _stale_update(service, record)
    now = datetime.now(timezone.utc)
    recovered = service.monitor_strategy_position(
        str(stale.id),
        Decimal("0.15570"),
        market_price_at=now,
        market_price_received_at=now,
        market_price_source="v2_feature_last_trade",
        now=now,
    )
    assert recovered.stop_loss == Decimal("0.15553")
    assert recovered.protection_fresh_data_recovered_at is not None
    assert ("PROTECTION_DATA_FRESH_RECOVERED", "DEMO_POSITION_OPEN") in (
        repo.saved_events
    )


def test_trailing_update_is_not_sent_with_stale_data() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    _stale_update(service, record)
    assert client.set_stop_calls == []


def test_safe_deferral_emits_no_cycle_failure() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    result = _stale_update(service, record)
    assert result.last_protection_verification["cycle_failure_emitted"] is False


def test_safe_deferral_has_no_persistence_failure() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, repo, record = _trailing_service(client)
    _stale_update(service, record)
    assert repo.saved_events
    assert service.repository is repo


def test_real_canary_stage_a_invokes_production_verifier_before_mutation() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, repo, record = _trailing_service(client)
    service.settings.demo_canary_enabled = True
    result = service.request_canary_stale_management_update(str(record.id))
    assert result is not None
    assert result.protection_data_state == (
        ProtectionDataState.PROTECTION_UPDATE_DEFERRED_STALE_PRICE
    )
    assert result.last_protection_verification["result"] == (
        "PROTECTION_UPDATE_DEFERRED_STALE_PRICE"
    )
    assert result.last_protection_verification["source"] == "REST"
    assert client.set_stop_calls == []
    assert (
        "PROTECTION_UPDATE_DEFERRED_STALE_PRICE",
        DemoExecutionState.DEMO_POSITION_OPEN.value,
    ) in repo.saved_events


def test_deferred_trailing_update_is_counted_separately() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    result = _stale_update(service, record)
    metrics = service.protection_data_metrics([result])
    assert metrics["deferred_trailing_updates"] == 1
    assert metrics["deferred_break_even_updates"] == 0


def test_deferred_break_even_update_is_counted_separately() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    result = service._defer_stale_management_update(
        record,
        observed_at=datetime.now(timezone.utc),
        market_price_source="v2_feature_last_trade",
        market_price_at=datetime.now(timezone.utc) - timedelta(seconds=16),
        market_price_received_at=datetime.now(timezone.utc),
        market_price_age_seconds=16,
        requested_update_type="break_even",
        defer_reason="fixture stale price",
    )
    assert service.protection_data_metrics([result])[
        "deferred_break_even_updates"
    ] == 1


def test_deferred_adaptive_exit_is_counted_separately() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    result = service._defer_stale_management_update(
        record,
        observed_at=datetime.now(timezone.utc),
        market_price_source="v2_strategy_feature",
        market_price_at=datetime.now(timezone.utc) - timedelta(seconds=16),
        market_price_received_at=datetime.now(timezone.utc),
        market_price_age_seconds=16,
        requested_update_type="adaptive_exit",
        defer_reason="fixture stale strategy feature",
    )
    assert service.protection_data_metrics([result])[
        "deferred_adaptive_exits"
    ] == 1


def test_repeated_stale_polls_form_one_typed_incident() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, repo, record = _trailing_service(client)
    first = _stale_update(service, record)
    _stale_update(service, first, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    typed = [
        event
        for event, _ in repo.saved_events
        if event == "PROTECTION_UPDATE_DEFERRED_STALE_PRICE"
    ]
    assert len(typed) == 1


def test_restart_preserves_deferred_management_state() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, repo, record = _trailing_service(client)
    deferred = _stale_update(service, record)
    restarted = DemoExecutionService(
        demo_settings(v2_protection_verification_delay_ms=0),
        repo,
        client,
        run_id=record.run_id,
    )
    restored = next(
        row for row in restarted.repository.load_demo_executions()
        if row.id == deferred.id
    )
    assert restored.protection_data_state == (
        ProtectionDataState.PROTECTION_UPDATE_DEFERRED_STALE_PRICE
    )


def test_malformed_timestamp_fails_closed() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    with pytest.raises(DemoSafetyError, match="timestamp is malformed"):
        service.monitor_strategy_position(
            str(record.id),
            Decimal("0.15570"),
            market_price_at="not-a-timestamp",  # type: ignore[arg-type]
            market_price_source="v2_feature_last_trade",
        )


def test_clock_inconsistency_remains_visible() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, repo, record = _trailing_service(client)
    now = datetime.now(timezone.utc)
    with pytest.raises(DemoSafetyError, match="clock inconsistency"):
        service.monitor_strategy_position(
            str(record.id),
            Decimal("0.15570"),
            market_price_at=now + timedelta(seconds=1),
            market_price_received_at=now,
            market_price_source="v2_feature_last_trade",
            now=now,
        )
    saved = repo.records[str(record.candidate_id)]
    assert saved.protection_data_state == ProtectionDataState.PROTECTION_DATA_CONFLICT


def test_cached_state_cannot_override_rest_protection() -> None:
    client = ProtectionReplayClient(["0.15553"])
    client.cached_snapshot = {"stopLoss": "0"}
    service, _, record = _trailing_service(client)
    result = _stale_update(service, record)
    assert result.protection_data_evidence["authoritative_source"] == "REST"
    assert result.protection_data_evidence[
        "existing_exchange_protection_remained_valid"
    ] is True


def test_authoritative_price_is_allowed_for_compatible_update() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    now = datetime.now(timezone.utc)
    result = service._update_and_verify_protection(
        record,
        rules=_wif_rules(),
        take_profit=record.take_profit,
        stop_loss=Decimal("0.15553"),
        verified_at=now,
        market_price=Decimal("0.15570"),
        market_price_source="authoritative_position_mark",
        market_price_at=now,
        market_price_received_at=now,
    )
    assert result.verified is True
    assert len(client.set_stop_calls) == 1


def test_unrelated_ticker_does_not_replace_stale_strategy_feature() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    result = _stale_update(service, record)
    assert result.protection_data_evidence["required_data_source"] == (
        "v2_feature_last_trade"
    )
    assert client.set_stop_calls == []


def test_management_price_uses_its_exact_ticker_timestamp() -> None:
    ticker_at = datetime.now(timezone.utc)
    trade_at = ticker_at - timedelta(seconds=12)
    received_at = ticker_at + timedelta(milliseconds=20)
    feature = SimpleNamespace(
        last_price=Decimal("0.15570"),
        timestamp=received_at,
        source_timestamps={"ticker": ticker_at, "trades": trade_at},
    )
    price, source_at, received, source = protection_management_price_input(
        feature
    )
    assert price == Decimal("0.15570")
    assert source_at == ticker_at
    assert source_at != trade_at
    assert received == received_at
    assert source == "v2_feature_ticker_last_price"


def test_cycle_311_replay_is_safe_deferral_not_failure() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, _, record = _trailing_service(client)
    incident = datetime.fromisoformat(
        FIXTURE["incident_at"].replace("Z", "+00:00")
    )
    result = service.monitor_strategy_position(
        str(record.id),
        Decimal("0.15570"),
        market_price_at=incident
        - timedelta(seconds=FIXTURE["calculated_age_at_incident_seconds"]),
        market_price_received_at=datetime.fromisoformat(
            FIXTURE["last_known_feature_snapshot_at"].replace("Z", "+00:00")
        ),
        market_price_source="v2_feature_last_trade",
        now=incident,
    )
    assert result.protection_data_state.value == FIXTURE["expected_state"]
    assert result.last_protection_verification["cycle_failure_emitted"] is (
        FIXTURE["expected_cycle_failure"]
    )


def test_true_unprotected_replay_still_fails() -> None:
    client = ProtectionReplayClient(["0.15553"])
    service, repo, record = _trailing_service(client)
    record.protection_confirmed = False
    repo.records[str(record.candidate_id)] = record
    with pytest.raises(DemoSafetyError, match="unprotected position"):
        _stale_update(service, record)
    assert repo.records[str(record.candidate_id)].protection_data_state == (
        ProtectionDataState.UNPROTECTED_CONFIRMED
    )


class _PendingClient(ProtectionReplayClient):
    def __init__(self, *, protected: bool) -> None:
        super().__init__(["0.15483"])
        self.protected = protected

    def get_positions(self, symbol=None, settle_coin=None):
        rows = super().get_positions(symbol, settle_coin)
        if rows and not self.protected:
            rows[0]["takeProfit"] = ""
            rows[0]["stopLoss"] = ""
        return rows


def test_initial_protection_pending_waits_inside_existing_bound() -> None:
    client = _PendingClient(protected=False)
    repo = MemoryRepository()
    record = _trailing_record().model_copy(update={
        "state": DemoExecutionState.DEMO_PROTECTION_PENDING,
        "protection_confirmed": False,
    })
    repo.records[str(record.candidate_id)] = record
    service = DemoExecutionService(
        demo_settings(
            v2_protection_verification_attempts=3,
            v2_protection_verification_delay_ms=200,
        ),
        repo,
        client,
        run_id=record.run_id,
    )
    result = service._defer_stale_management_update(
        record,
        observed_at=record.created_at,
        market_price_source="v2_feature_last_trade",
        market_price_at=record.created_at - timedelta(seconds=16),
        market_price_received_at=record.created_at,
        market_price_age_seconds=16,
        requested_update_type="initial_protection",
        defer_reason="fixture stale price",
    )
    assert result.protection_data_state == (
        ProtectionDataState.PROTECTION_PENDING_FRESHNESS_WAIT
    )


def test_initial_protection_succeeds_before_deadline() -> None:
    client = _PendingClient(protected=True)
    repo = MemoryRepository()
    record = _trailing_record().model_copy(update={
        "state": DemoExecutionState.DEMO_PROTECTION_PENDING,
        "protection_confirmed": False,
    })
    repo.records[str(record.candidate_id)] = record
    service = DemoExecutionService(
        demo_settings(), repo, client, run_id=record.run_id
    )
    result = service._defer_stale_management_update(
        record,
        observed_at=record.created_at,
        market_price_source="v2_feature_last_trade",
        market_price_at=record.created_at - timedelta(seconds=16),
        market_price_received_at=record.created_at,
        market_price_age_seconds=16,
        requested_update_type="initial_protection",
        defer_reason="fixture stale price",
    )
    assert result.protection_confirmed is True
    assert result.protection_data_state == ProtectionDataState.PROTECTION_DATA_FRESH


def test_initial_protection_deadline_expiry_fails_fast() -> None:
    client = _PendingClient(protected=False)
    repo = MemoryRepository()
    record = _trailing_record().model_copy(update={
        "state": DemoExecutionState.DEMO_PROTECTION_PENDING,
        "protection_confirmed": False,
        "protection_data_deferred_at": datetime.now(timezone.utc)
        - timedelta(seconds=2),
    })
    repo.records[str(record.candidate_id)] = record
    service = DemoExecutionService(
        demo_settings(
            v2_protection_verification_attempts=3,
            v2_protection_verification_delay_ms=200,
        ),
        repo,
        client,
        run_id=record.run_id,
    )
    with pytest.raises(DemoSafetyError, match="unprotected position"):
        service._defer_stale_management_update(
            record,
            observed_at=datetime.now(timezone.utc),
            market_price_source="v2_feature_last_trade",
            market_price_at=datetime.now(timezone.utc) - timedelta(seconds=16),
            market_price_received_at=datetime.now(timezone.utc),
            market_price_age_seconds=16,
            requested_update_type="initial_protection",
            defer_reason="fixture stale price",
        )
