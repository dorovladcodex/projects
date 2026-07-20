from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.db.persistence import PersistenceRepository, normalize_database_url
from app.models import DemoExecutionRecord, DemoExecutionState, Symbol


DEMO_READ_ONLY_REST_URL = "https://api-demo.bybit.com"
RESOLVED_STATES = {
    DemoExecutionState.DEMO_CLOSED,
    DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE,
    DemoExecutionState.DEMO_NOT_SUBMITTED,
    DemoExecutionState.DEMO_ORDER_CANCELLED,
    DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION,
    DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
    DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED,
}


class DemoDiagnosticsError(RuntimeError):
    pass


@dataclass(frozen=True)
class DemoDiagnosticsConfig:
    database_url: str
    api_key: str
    api_secret: str
    rest_url: str = DEMO_READ_ONLY_REST_URL
    universe_symbols: tuple[str, ...] = (
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT",
        "LTCUSDT", "TONUSDT", "PEPEUSDT", "SHIBUSDT", "WIFUSDT",
        "BONKUSDT", "FLOKIUSDT",
    )

    @classmethod
    def load(
        cls,
        environment: Mapping[str, str] | None = None,
        env_path: Path | None = None,
    ) -> "DemoDiagnosticsConfig":
        values = dict(environment or os.environ)
        file_values = _read_env_file(env_path or Path(".env"))

        def required(name: str) -> str:
            value = values.get(name) or file_values.get(name) or ""
            if not value:
                raise DemoDiagnosticsError(f"{name} is required")
            return value

        database_url = normalize_database_url(required("DATABASE_URL"))
        database_url = database_url.replace("@db:", "@127.0.0.1:")
        rest_url = (
            values.get("BYBIT_PRIVATE_DEMO_BASE_URL")
            or file_values.get("BYBIT_PRIVATE_DEMO_BASE_URL")
            or DEMO_READ_ONLY_REST_URL
        ).rstrip("/")
        if rest_url != DEMO_READ_ONLY_REST_URL:
            raise DemoDiagnosticsError(
                "read-only diagnostics require exact api-demo.bybit.com domain"
            )
        raw_symbols = (
            values.get("V2_UNIVERSE_SYMBOLS")
            or file_values.get("V2_UNIVERSE_SYMBOLS")
            or ""
        )
        universe_symbols = _parse_symbol_list(raw_symbols) or cls.universe_symbols
        return cls(
            database_url=database_url,
            api_key=required("BYBIT_API_KEY"),
            api_secret=required("BYBIT_API_SECRET"),
            rest_url=rest_url,
            universe_symbols=universe_symbols,
        )


class ReadOnlyBybitDemoClient:
    """Minimal signed GET-only client; mutation methods intentionally do not exist."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = DEMO_READ_ONLY_REST_URL,
        timeout_seconds: float = 10,
        http_get: Callable[[str, dict[str, str], dict[str, str], float], dict[str, Any]]
        | None = None,
    ) -> None:
        if base_url.rstrip("/") != DEMO_READ_ONLY_REST_URL:
            raise DemoDiagnosticsError("non-Demo REST domain is forbidden")
        if not api_key or not api_secret:
            raise DemoDiagnosticsError("Demo read-only credentials are required")
        self.base_url = DEMO_READ_ONLY_REST_URL
        self._api_key = api_key
        self._api_secret = api_secret
        self.timeout_seconds = timeout_seconds
        self._http_get = http_get or _read_only_url_get

    def verify(self) -> None:
        self._get(
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": "USDT"},
        )

    def get_open_orders(self) -> list[dict[str, Any]]:
        return self._paginate(
            "/v5/order/realtime",
            {"category": "linear", "settleCoin": "USDT", "openOnly": "0"},
            "orderId",
        )

    def get_positions(self, symbol: Symbol) -> list[dict[str, Any]]:
        return self._paginate(
            "/v5/position/list",
            {"category": "linear", "symbol": symbol.value},
            "positionIdx",
        )

    def get_usdt_positions(self) -> list[dict[str, Any]]:
        return self._paginate(
            "/v5/position/list",
            {"category": "linear", "settleCoin": "USDT"},
            "symbol",
        )

    def get_order_history(self, symbol: Symbol) -> list[dict[str, Any]]:
        return self._paginate(
            "/v5/order/history",
            {"category": "linear", "symbol": symbol.value},
            "orderId",
        )

    def get_executions(self, symbol: Symbol) -> list[dict[str, Any]]:
        return self._paginate(
            "/v5/execution/list",
            {"category": "linear", "symbol": symbol.value},
            "execId",
        )

    def get_closed_pnl(self, symbol: Symbol) -> list[dict[str, Any]]:
        return self._paginate(
            "/v5/position/closed-pnl",
            {"category": "linear", "symbol": symbol.value},
            "orderId",
        )

    def get_transaction_log(self) -> list[dict[str, Any]]:
        return self._paginate(
            "/v5/account/transaction-log",
            {"accountType": "UNIFIED", "category": "linear", "currency": "USDT"},
            "id",
        )

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        timestamp = str(int(time.time() * 1000))
        query = urlencode(sorted(params.items()))
        recv_window = "5000"
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            f"{timestamp}{self._api_key}{recv_window}{query}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        payload = self._http_get(
            f"{self.base_url}{path}",
            params,
            {
                "X-BAPI-API-KEY": self._api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "X-BAPI-SIGN": signature,
            },
            self.timeout_seconds,
        )
        if int(payload.get("retCode", -1)) != 0:
            raise DemoDiagnosticsError(
                f"Bybit read-only request failed: retCode={payload.get('retCode')}"
            )
        return payload

    def _paginate(
        self, path: str, params: dict[str, str], identity_field: str
    ) -> list[dict[str, Any]]:
        original = dict(params)
        cursor = ""
        seen_cursors: set[str] = set()
        rows: dict[str, dict[str, Any]] = {}
        while True:
            query = dict(original)
            if cursor:
                query["cursor"] = cursor
            payload = self._get(path, query)
            result = payload.get("result") or {}
            for index, item in enumerate(result.get("list") or []):
                if not isinstance(item, dict):
                    continue
                identity = str(item.get(identity_field) or f"row:{index}:{cursor}")
                rows[identity] = item
            next_cursor = str(result.get("nextPageCursor") or "")
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return list(rows.values())


@dataclass
class DemoDiagnosticsResult:
    kill_switch: dict[str, Any]
    latest_execution: DemoExecutionRecord | None
    bot_owned_open_orders: list[dict[str, Any]]
    unrelated_open_orders: list[dict[str, Any]]
    positions: dict[str, dict[str, str]]
    unresolved_executions: list[DemoExecutionRecord]
    failures: list[str]
    latest_order_history: list[dict[str, Any]]
    latest_execution_events: list[dict[str, Any]]
    newest_execution: DemoExecutionRecord | None
    bot_owned_entry_orders: list[dict[str, Any]]
    bot_owned_close_orders: list[dict[str, Any]]
    bot_owned_tp_orders: list[dict[str, Any]]
    bot_owned_sl_orders: list[dict[str, Any]]
    position_ownership: dict[str, list[str]]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class DemoRecoveryReadiness:
    selected_execution_id: str
    selected_execution_state: str | None
    accepted_terminal_states: tuple[str, ...]
    cleanup_result_matches: bool
    linked_activation_id: str | None
    reason_classification: str
    remote_state_verified: bool
    remote_positions_flat: bool
    bot_owned_orders_zero: bool
    durable_executions_resolved: bool
    latest_execution_terminal: bool
    latest_execution_safely_closed: bool
    repair_audit_complete: bool
    activation_link_valid: bool
    recoverable_latch: bool
    blockers: tuple[str, ...]


def evaluate_demo_recovery_readiness(
    result: DemoDiagnosticsResult, execution_id: str
) -> DemoRecoveryReadiness:
    """Single fail-closed policy shared by diagnostics and reset."""
    blockers = list(result.failures)
    latest = result.latest_execution
    kill = result.kill_switch
    accepted = tuple(sorted(state.value for state in RESOLVED_STATES))
    terminal = bool(latest and latest.state in RESOLVED_STATES)
    cleanup_matches = bool(
        latest
        and latest.cleanup_result == "remote position flat and bot-owned orders zero"
    )
    remote_positions_flat = all(
        not _positive(position.get("size")) for position in result.positions.values()
    )
    bot_orders_zero = not result.bot_owned_open_orders
    durable_resolved = not result.unresolved_executions
    remote_verified = result.passed and remote_positions_flat and bot_orders_zero

    audit_types = {
        str(event.get("event_type") or "")
        for event in result.latest_execution_events
    }
    required_repair_audit = {
        "READ_ONLY_RECONCILIATION_COMPLETED",
        "FINAL_REMOTE_STATE_FLAT",
    }
    repair_complete = bool(
        required_repair_audit.issubset(audit_types)
        and audit_types.intersection({
            "EXECUTION_REPAIR_APPLIED", "EXTERNAL_CLOSE_ATTRIBUTED",
            "EXECUTION_FINALIZED_FLAT_VERIFIED",
        })
    )
    history = result.latest_order_history
    entry_rows = [row for row in history if latest and _matches_order(
        row, latest.order_id, latest.order_link_id
    )]
    close_rows = [row for row in history if latest and _matches_order(
        row, latest.close_order_id, latest.close_order_link_id
    )]
    terminal_statuses = {"Filled", "Cancelled", "Rejected", "Deactivated"}
    orders_terminal = bool(
        latest
        and (not latest.order_id or entry_rows)
        and (not latest.close_order_id or close_rows)
        and all(str(row.get("orderStatus") or "") in terminal_statuses
                for row in [*entry_rows, *close_rows])
    )
    filled_entry = any(str(row.get("orderStatus") or "") == "Filled" for row in entry_rows)
    close_reduce_only = bool(
        not filled_entry
        or any(
            str(row.get("orderStatus") or "") == "Filled"
            and str(row.get("reduceOnly") or "").lower() == "true"
            for row in close_rows
        )
    )

    events = list(kill.get("events") or [])
    linked = next((event for event in reversed(events)
                   if str(event.get("execution_id") or "") == execution_id), None)
    # Historical activations predated execution linkage. Complete repair audit,
    # exact latest execution selection, and one classified protection incident
    # form guarded evidence; confirmed reset appends an explicit link audit.
    activation_events = [
        event for event in events
        if event.get("event_type") in {
            "KILL_SWITCH_ACTIVATED", "LEGACY_ACTIVATION",
            "KILL_SWITCH_REASON_ADDED",
        }
    ]
    temporal_activation = next((
        event for event in reversed(activation_events)
        if latest and _event_within_execution_window(event, latest)
    ), None)
    repair_identity_matches = bool(
        latest and latest.run_id
        and entry_rows and (close_rows or not latest.close_order_id)
        and any(
            event.get("run_id") in {None, latest.run_id}
            and event.get("order_id") in {None, latest.order_id}
            and event.get("close_order_id") in {None, latest.close_order_id}
            for event in result.latest_execution_events
            if event.get("event_type") == "EXECUTION_REPAIR_APPLIED"
        )
    )
    inferred_link = bool(
        latest and str(latest.id) == execution_id and repair_complete
        and repair_identity_matches and temporal_activation is not None
    )
    activation_link_valid = linked is not None or inferred_link
    linked_activation_id = (
        str((linked.get("payload") or {}).get("activation_id") or linked.get("id"))
        if linked else
        ("repair-audit-inference" if inferred_link else None)
    )

    reasons = [str(reason) for reason in kill.get("reasons") or []]
    reason_classes = [_classify_kill_reason(reason) for reason in reasons]
    reason_classification = ",".join(sorted(set(reason_classes))) or "none"
    reasons_recoverable = bool(reasons) and all(
        item in {
            "resolved_protection_incident",
            "restart_protection_ownership_incident",
        }
        for item in reason_classes
    )
    if "restart_protection_ownership_incident" in reason_classes:
        reasons_recoverable = bool(
            reasons_recoverable and latest
            and latest.state == DemoExecutionState.DEMO_CLOSED_EXTERNALLY
            and repair_complete and remote_verified and activation_link_valid
        )

    safely_closed = False
    if latest and terminal and cleanup_matches:
        if latest.state == DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION:
            safely_closed = bool(
                str(latest.id) == execution_id
                and orders_terminal and close_reduce_only and repair_complete
                and remote_positions_flat and bot_orders_zero and durable_resolved
            )
        elif latest.state == DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE:
            safely_closed = bool(latest.failure_reason and remote_verified)
        else:
            safely_closed = remote_verified

    if not kill.get("active"):
        blockers.append("Demo kill switch is not active")
    if latest is None or str(latest.id) != execution_id:
        blockers.append("confirmation execution ID is not the latest Demo execution")
    if not terminal:
        blockers.append("latest execution state is not terminal")
    if not safely_closed:
        blockers.append("latest execution is not safely closed")
    if latest and latest.state == DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION:
        if not repair_complete:
            blockers.append("interruption repair audit is incomplete")
        if not orders_terminal:
            blockers.append("known entry/close orders are not terminal")
        if not close_reduce_only:
            blockers.append("filled entry has no terminal reduce-only close")
    if not activation_link_valid:
        blockers.append("kill-switch activation is not linked to the execution")
    if not reasons_recoverable:
        blockers.append("kill-switch incident is not recoverable")
    unique = tuple(dict.fromkeys(blockers))
    return DemoRecoveryReadiness(
        selected_execution_id=execution_id,
        selected_execution_state=latest.state.value if latest else None,
        accepted_terminal_states=accepted,
        cleanup_result_matches=cleanup_matches,
        linked_activation_id=linked_activation_id,
        reason_classification=reason_classification,
        remote_state_verified=remote_verified,
        remote_positions_flat=remote_positions_flat,
        bot_owned_orders_zero=bot_orders_zero,
        durable_executions_resolved=durable_resolved,
        latest_execution_terminal=terminal,
        latest_execution_safely_closed=safely_closed,
        repair_audit_complete=repair_complete,
        activation_link_valid=activation_link_valid,
        recoverable_latch=bool(kill.get("active")) and reasons_recoverable and not unique,
        blockers=unique,
    )


def validate_recoverable_demo_latch(
    result: DemoDiagnosticsResult, execution_id: str
) -> list[str]:
    return list(evaluate_demo_recovery_readiness(result, execution_id).blockers)


def run_demo_diagnostics(
    config: DemoDiagnosticsConfig,
    *,
    repository: PersistenceRepository | None = None,
    client: ReadOnlyBybitDemoClient | None = None,
) -> DemoDiagnosticsResult:
    repo = repository or PersistenceRepository(config.database_url, create_schema=False)
    if not repo.available:
        raise DemoDiagnosticsError("database persistence is unavailable")
    read_client = client or ReadOnlyBybitDemoClient(
        config.api_key, config.api_secret, base_url=config.rest_url
    )
    read_client.verify()
    kill_switch = repo.load_demo_kill_switch() or {
        "active": False, "reasons": [], "updated_at": None,
        "activated_at": None, "activation_count": 0, "events": [],
    }
    executions = sorted(
        repo.load_demo_executions(), key=lambda item: item.created_at, reverse=True
    )
    newest = executions[0] if executions else None
    unresolved = [item for item in executions if not _execution_is_resolved(item)]
    linked_ids = {
        str(event.get("execution_id") or "")
        for event in kill_switch.get("events") or []
        if event.get("active") and event.get("execution_id")
    }
    linked_unresolved = [item for item in unresolved if str(item.id) in linked_ids]
    failed_terminal = [
        item for item in executions
        if _execution_is_resolved(item) and item.failure_reason
    ]
    terminal = [item for item in executions if _execution_is_resolved(item)]
    latest = (
        (linked_unresolved[0] if linked_unresolved else None)
        or (unresolved[0] if unresolved else None)
        or (failed_terminal[0] if failed_terminal else None)
        or (terminal[0] if terminal else None)
    )

    known_links = {
        link
        for item in executions
        for link in (item.order_link_id, item.close_order_link_id)
        if link
    }
    stored_prefixes = {
        f"{link.rsplit('-', 2)[0]}-"
        for link in known_links
        if len(link.rsplit("-", 2)) == 3
    }
    remote_orders = read_client.get_open_orders()
    positions: dict[str, dict[str, str]] = {
        symbol: {
            "size": "0", "side": "", "position_idx": "0",
            "take_profit": "", "stop_loss": "",
        }
        for symbol in config.universe_symbols
    }
    active_position_symbols: set[str] = set()
    configured = set(config.universe_symbols)
    configured.update(item.symbol.value for item in unresolved)
    if hasattr(read_client, "get_usdt_positions"):
        position_rows = read_client.get_usdt_positions()
    else:
        position_rows = [
            row
            for symbol_value in sorted(configured)
            for row in read_client.get_positions(Symbol(symbol_value))
        ]
    for symbol_value in sorted(configured):
        rows = [
            item for item in position_rows
            if str(item.get("symbol") or "") == symbol_value
        ]
        active = next((item for item in rows if _positive(item.get("size"))), None)
        positions[symbol_value] = {
            "size": str((active or {}).get("size") or "0"),
            "side": str((active or {}).get("side") or ""),
            "position_idx": str((active or {}).get("positionIdx") or "0"),
            "take_profit": str((active or {}).get("takeProfit") or ""),
            "stop_loss": str((active or {}).get("stopLoss") or ""),
        }
        if active:
            active_position_symbols.add(symbol_value)

    classified = classify_demo_open_orders(
        remote_orders, executions, positions, known_links, stored_prefixes
    )
    bot_entry_orders = classified["entry"]
    bot_close_orders = classified["close"]
    bot_tp_orders = classified["take_profit"]
    bot_sl_orders = classified["stop_loss"]
    bot_orders = [
        *bot_entry_orders, *bot_close_orders, *bot_tp_orders, *bot_sl_orders,
    ]
    unrelated_orders = classified["unrelated"]
    position_ownership = {
        symbol: [
            str(item.id) for item in unresolved
            if item.symbol.value == symbol
            and _decimal_value(positions[symbol]["size"]) == item.accepted_quantity
            and positions[symbol]["side"].upper()
            == ("BUY" if item.side.value == "BUY" else "SELL")
        ]
        for symbol in active_position_symbols
    }

    failures: list[str] = []
    if bot_orders:
        failures.append("bot-owned Demo order exists")
    if active_position_symbols:
        failures.append("Demo position exists; ownership cannot be safely excluded")
    if unresolved:
        failures.append("durable Demo execution is unresolved")
    if latest and latest.state in RESOLVED_STATES:
        if latest.symbol.value in active_position_symbols or bot_orders:
            failures.append("remote and durable Demo states disagree")
    order_history = (
        read_client.get_order_history(latest.symbol)
        if latest is not None and hasattr(read_client, "get_order_history") else []
    )
    load_events = getattr(repo, "load_demo_execution_events", None)
    execution_events = (
        load_events(str(latest.id))
        if latest is not None and callable(load_events) else []
    )
    return DemoDiagnosticsResult(
        kill_switch=kill_switch,
        latest_execution=latest,
        bot_owned_open_orders=bot_orders,
        unrelated_open_orders=unrelated_orders,
        positions=positions,
        unresolved_executions=unresolved,
        failures=failures,
        latest_order_history=order_history,
        latest_execution_events=execution_events,
        newest_execution=newest,
        bot_owned_entry_orders=bot_entry_orders,
        bot_owned_close_orders=bot_close_orders,
        bot_owned_tp_orders=bot_tp_orders,
        bot_owned_sl_orders=bot_sl_orders,
        position_ownership=position_ownership,
    )


def format_demo_diagnostics(result: DemoDiagnosticsResult) -> str:
    latest = result.latest_execution
    newest = result.newest_execution
    reasons = result.kill_switch.get("reasons") or []
    lines = [
        f"DEMO KILL SWITCH ACTIVE: {str(bool(result.kill_switch.get('active'))).lower()}",
        "DEMO KILL SWITCH REASONS: " + ("; ".join(reasons) or "none"),
        "DEMO KILL SWITCH ACTIVATED AT: "
        + _display_time(result.kill_switch.get("activated_at")),
        "DEMO KILL SWITCH ACTIVATION COUNT: "
        + str(result.kill_switch.get("activation_count") or 0),
        "LATEST DEMO EXECUTION ID: " + (str(newest.id) if newest else "none"),
        "LATEST DEMO EXECUTION STATE: " + (newest.state.value if newest else "none"),
        "ACTIVE INCIDENT EXECUTION ID: " + (str(latest.id) if latest else "none"),
        "LATEST DEMO FAILURE REASON: " + ((latest.failure_reason or "none") if latest else "none"),
        "LATEST DEMO CLEANUP RESULT: " + ((latest.cleanup_result or "none") if latest else "none"),
        f"REMOTE BOT-OWNED ENTRY ORDERS: {len(result.bot_owned_entry_orders)}",
        f"REMOTE BOT-OWNED CLOSE ORDERS: {len(result.bot_owned_close_orders)}",
        f"REMOTE BOT-OWNED TP ORDERS: {len(result.bot_owned_tp_orders)}",
        f"REMOTE BOT-OWNED SL ORDERS: {len(result.bot_owned_sl_orders)}",
        f"REMOTE BOT-OWNED OPEN ORDERS: {len(result.bot_owned_open_orders)}",
        f"REMOTE UNRELATED OPEN ORDERS: {len(result.unrelated_open_orders)}",
        "REMOTE SYMBOLS CHECKED: " + ", ".join(sorted(result.positions)),
        "REMOTE NON-ZERO POSITIONS: " + (
            json.dumps({
                symbol: {
                    **position,
                    "execution_ids": result.position_ownership.get(symbol, []),
                }
                for symbol, position in result.positions.items()
                if _positive(position.get("size"))
            }, sort_keys=True) or "none"
        ),
        "REMOTE ORDERS BY SYMBOL AND OWNERSHIP: "
        + json.dumps(_orders_by_symbol_and_ownership(result), sort_keys=True),
        "UNRESOLVED EXECUTION OWNERSHIP: "
        + json.dumps(_unresolved_execution_ownership(result), sort_keys=True),
        "UNRESOLVED DURABLE EXECUTIONS: "
        + (", ".join(str(item.id) for item in result.unresolved_executions) or "none"),
        "READ-ONLY DIAGNOSTICS: " + ("PASS" if result.passed else "FAIL"),
    ]
    if result.failures:
        lines.append("FAILURES: " + "; ".join(result.failures))
    if latest is not None and result.kill_switch.get("active"):
        readiness = evaluate_demo_recovery_readiness(result, str(latest.id))
        lines.extend(format_demo_recovery_readiness(readiness).splitlines())
    return "\n".join(lines)


def format_demo_recovery_readiness(readiness: DemoRecoveryReadiness) -> str:
    def verdict(value: bool, detail: str = "") -> str:
        return ("PASS" if value else "FAIL") + (f" ({detail})" if detail else "")

    return "\n".join([
        f"SELECTED EXECUTION ID: {readiness.selected_execution_id}",
        "SELECTED EXECUTION STATE: " + str(readiness.selected_execution_state or "none"),
        "ACCEPTED TERMINAL STATES: " + ", ".join(readiness.accepted_terminal_states),
        "CLEANUP RESULT COMPARISON: " + verdict(readiness.cleanup_result_matches),
        "LINKED KILL-SWITCH ACTIVATION ID: "
        + str(readiness.linked_activation_id or "none"),
        "REASON CLASSIFICATION: " + readiness.reason_classification,
        "UNRESOLVED EXECUTION RESULT: "
        + verdict(readiness.durable_executions_resolved),
        "REMOTE FLAT RESULT: " + verdict(readiness.remote_positions_flat),
        "LATEST EXECUTION TERMINAL: "
        + verdict(readiness.latest_execution_terminal,
                  f"state={readiness.selected_execution_state}"),
        "REPAIR AUDIT COMPLETE: " + verdict(readiness.repair_audit_complete),
        "REMOTE POSITION FLAT: " + verdict(readiness.remote_positions_flat),
        "BOT-OWNED ORDERS ZERO: " + verdict(readiness.bot_owned_orders_zero),
        "ACTIVATION LINK: " + verdict(readiness.activation_link_valid),
        "RECOVERABLE INCIDENT: " + verdict(readiness.recoverable_latch),
        "BLOCKERS: " + ("; ".join(readiness.blockers) or "none"),
    ])


def _execution_is_resolved(record: DemoExecutionRecord) -> bool:
    if record.state == DemoExecutionState.DEMO_CLOSED:
        return True
    if record.state in {
        DemoExecutionState.DEMO_NOT_SUBMITTED,
        DemoExecutionState.DEMO_ORDER_CANCELLED,
        DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION,
        DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
        DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED,
    }:
        return bool(record.cleanup_result)
    return bool(
        record.state == DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE
        and record.failure_reason
        and record.cleanup_result == "remote position flat and bot-owned orders zero"
    )


def _is_bot_owned_order(
    item: dict[str, Any], known_links: set[str], stored_prefixes: set[str]
) -> bool:
    link = str(item.get("orderLinkId") or "")
    return bool(
        link and (link in known_links or any(link.startswith(p) for p in stored_prefixes))
    )


def classify_demo_open_orders(
    orders: list[dict[str, Any]],
    executions: list[DemoExecutionRecord],
    positions: dict[str, dict[str, str]],
    known_links: set[str] | None = None,
    stored_prefixes: set[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Classify every order once using exact IDs or full protection evidence."""

    result = {
        "entry": [], "close": [], "take_profit": [], "stop_loss": [],
        "unrelated": [],
    }
    links = known_links or set()
    prefixes = stored_prefixes or set()
    for order in orders:
        order_id = str(order.get("orderId") or "")
        order_link = str(order.get("orderLinkId") or "")
        if any(
            (order_id and order_id == item.order_id)
            or (order_link and order_link == item.order_link_id)
            for item in executions
        ):
            result["entry"].append(order)
            continue
        if any(
            (order_id and order_id == item.close_order_id)
            or (order_link and order_link == item.close_order_link_id)
            for item in executions
        ):
            result["close"].append(order)
            continue
        exact_tp = [item for item in executions if order_id and item.tp_order_id == order_id]
        exact_sl = [item for item in executions if order_id and item.sl_order_id == order_id]
        if len(exact_tp) == 1:
            result["take_profit"].append(order)
            continue
        if len(exact_sl) == 1:
            result["stop_loss"].append(order)
            continue
        matches = [
            item for item in executions
            if item.state not in RESOLVED_STATES
            and _matches_generated_protection(order, item, positions)
        ]
        if len(matches) == 1:
            key = (
                "take_profit"
                if str(order.get("stopOrderType") or "") == "TakeProfit"
                else "stop_loss"
            )
            result[key].append(order)
            continue
        if _is_bot_owned_order(order, links, prefixes):
            result["entry"].append(order)
            continue
        result["unrelated"].append(order)
    return result


def _matches_generated_protection(
    order: dict[str, Any],
    execution: DemoExecutionRecord,
    positions: dict[str, dict[str, str]],
) -> bool:
    symbol = execution.symbol.value
    position = positions.get(symbol) or {}
    stop_type = str(order.get("stopOrderType") or "")
    if stop_type not in {"TakeProfit", "StopLoss"}:
        return False
    if str(order.get("createType") or "") != (
        "CreateByTakeProfit" if stop_type == "TakeProfit" else "CreateByStopLoss"
    ):
        return False
    if str(order.get("symbol") or "") != symbol:
        return False
    if str(order.get("side") or "").upper() != (
        "SELL" if execution.side.value == "BUY" else "BUY"
    ):
        return False
    if str(order.get("reduceOnly") or "").lower() != "true":
        return False
    if str(order.get("closeOnTrigger") or "").lower() != "true":
        return False
    position_idx = int(order.get("positionIdx") or 0)
    if position_idx != execution.protection_position_idx:
        return False
    if position_idx != int(position.get("position_idx") or 0):
        return False
    if _decimal_value(order.get("qty")) != execution.accepted_quantity:
        return False
    if _decimal_value(position.get("size")) != execution.accepted_quantity:
        return False
    if str(position.get("side") or "").upper() != (
        "BUY" if execution.side.value == "BUY" else "SELL"
    ):
        return False
    expected_trigger = (
        execution.take_profit if stop_type == "TakeProfit" else execution.stop_loss
    )
    if expected_trigger is None or _decimal_value(
        order.get("triggerPrice")
    ) != expected_trigger:
        return False
    return (
        execution.take_profit is not None
        and execution.stop_loss is not None
        and _decimal_value(position.get("take_profit")) == execution.take_profit
        and _decimal_value(position.get("stop_loss")) == execution.stop_loss
    )


def _orders_by_symbol_and_ownership(
    result: DemoDiagnosticsResult,
) -> dict[str, dict[str, list[str]]]:
    groups: dict[str, dict[str, list[str]]] = {}
    for label, rows in (
        ("entry", result.bot_owned_entry_orders),
        ("close", result.bot_owned_close_orders),
        ("take_profit", result.bot_owned_tp_orders),
        ("stop_loss", result.bot_owned_sl_orders),
        ("unrelated", result.unrelated_open_orders),
    ):
        for row in rows:
            symbol = str(row.get("symbol") or "UNKNOWN")
            groups.setdefault(symbol, {}).setdefault(label, []).append(
                str(row.get("orderId") or "unknown")
            )
    return groups


def _unresolved_execution_ownership(
    result: DemoDiagnosticsResult,
) -> dict[str, dict[str, Any]]:
    tp_ids = {
        str(item.get("orderId") or "") for item in result.bot_owned_tp_orders
    }
    sl_ids = {
        str(item.get("orderId") or "") for item in result.bot_owned_sl_orders
    }
    return {
        str(item.id): {
            "symbol": item.symbol.value,
            "state": item.state.value,
            "position_owned": str(item.id) in result.position_ownership.get(
                item.symbol.value, []
            ),
            "protection_confirmed": item.protection_confirmed,
            "tp_order_id": item.tp_order_id,
            "sl_order_id": item.sl_order_id,
            "tp_order_open": bool(item.tp_order_id and item.tp_order_id in tp_ids),
            "sl_order_open": bool(item.sl_order_id and item.sl_order_id in sl_ids),
        }
        for item in result.unresolved_executions
    }


def _decimal_value(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        raise DemoDiagnosticsError("Bybit returned an invalid numeric value")


def _parse_symbol_list(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return tuple(str(item).upper() for item in parsed if str(item).strip())
    except json.JSONDecodeError:
        pass
    return tuple(
        item.strip().upper() for item in value.split(",") if item.strip()
    )


def _matches_order(
    item: dict[str, Any], order_id: str | None, order_link_id: str | None
) -> bool:
    return bool(
        (order_id and str(item.get("orderId") or "") == order_id)
        or (order_link_id and str(item.get("orderLinkId") or "") == order_link_id)
    )


def _classify_kill_reason(reason: str) -> str:
    normalized = " ".join(reason.lower().split())
    risk_tokens = ("daily", "weekly", "drawdown")
    unsafe_tokens = (
        "unknown", "uncertain", "unattributed", "mismatch",
        "missing remotely", "duplicate",
    )
    if any(token in normalized for token in risk_tokens):
        return "risk_limit"
    if any(token in normalized for token in unsafe_tokens):
        if normalized == "unattributed active demo order for btcusdt":
            return "restart_protection_ownership_incident"
        return "unknown_exchange_state"
    protection_tokens = (
        "position update has no tp/sl",
        "not modified",
        "can not set tp/sl/ts for zero position",
    )
    if normalized.startswith("unprotected position:") and any(
        token in normalized for token in protection_tokens
    ):
        return "resolved_protection_incident"
    return "unknown_protection_incident"


def _positive(value: object) -> bool:
    try:
        return float(str(value or "0")) > 0
    except ValueError:
        raise DemoDiagnosticsError("Bybit returned an invalid position size")


def _display_time(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "none")


def _event_within_execution_window(
    event: dict[str, Any], execution: DemoExecutionRecord
) -> bool:
    value = event.get("created_at")
    if not isinstance(value, datetime):
        try:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return False
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    created = execution.created_at
    created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    updated = execution.updated_at
    updated = updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
    # Five minutes allows exchange/DB timestamp skew without linking unrelated
    # historical incidents. The repaired execution's updated_at is the upper
    # bound, so future activations cannot be inferred into this incident.
    from datetime import timedelta
    return created - timedelta(minutes=5) <= value <= updated


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _read_only_url_get(
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    request = Request(
        f"{url}?{urlencode(sorted(params.items()))}", headers=headers, method="GET"
    )
    with urlopen(request, timeout=timeout) as response:  # nosec B310: exact domain guarded
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise DemoDiagnosticsError("Bybit returned a non-object response")
    return payload
