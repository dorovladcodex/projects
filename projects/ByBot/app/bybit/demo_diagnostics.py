from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
}


class DemoDiagnosticsError(RuntimeError):
    pass


@dataclass(frozen=True)
class DemoDiagnosticsConfig:
    database_url: str
    api_key: str
    api_secret: str
    rest_url: str = DEMO_READ_ONLY_REST_URL

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
        return cls(
            database_url=database_url,
            api_key=required("BYBIT_API_KEY"),
            api_secret=required("BYBIT_API_SECRET"),
            rest_url=rest_url,
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

    @property
    def passed(self) -> bool:
        return not self.failures


def validate_recoverable_demo_latch(
    result: DemoDiagnosticsResult, execution_id: str
) -> list[str]:
    """Return fail-closed reset blockers without mutating any state."""
    blockers = list(result.failures)
    latest = result.latest_execution
    kill = result.kill_switch
    if not kill.get("active"):
        blockers.append("Demo kill switch is not active")
    if latest is None or str(latest.id) != execution_id:
        blockers.append("confirmation execution ID is not the latest Demo execution")
    elif (
        latest.state != DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE
        or latest.cleanup_result != "remote position flat and bot-owned orders zero"
        or latest.failure_reason != "local position-open state timeout"
    ):
        blockers.append("latest failed execution is not safely resolved")
    reasons = [str(reason) for reason in kill.get("reasons") or []]
    forbidden_tokens = {
        "unknown", "uncertain", "unattributed", "mismatch", "missing remotely",
        "duplicate", "daily", "weekly", "drawdown",
    }
    if any(token in reason.lower() for reason in reasons for token in forbidden_tokens):
        blockers.append("kill-switch reason is not automatically recoverable")
    recoverable_reason = "unprotected position: position update has no TP/SL"
    if not reasons or any(reason != recoverable_reason for reason in reasons):
        blockers.append("recoverable Demo latch was not proven")
    return list(dict.fromkeys(blockers))


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
        repo.load_demo_executions(), key=lambda item: item.updated_at, reverse=True
    )
    latest = executions[0] if executions else None
    unresolved = [item for item in executions if not _execution_is_resolved(item)]

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
    bot_orders = [
        item for item in remote_orders
        if _is_bot_owned_order(item, known_links, stored_prefixes)
    ]
    unrelated_orders = [item for item in remote_orders if item not in bot_orders]

    positions: dict[str, dict[str, str]] = {}
    active_position_symbols: set[str] = set()
    for symbol in (Symbol.BTCUSDT, Symbol.ETHUSDT):
        rows = read_client.get_positions(symbol)
        active = next((item for item in rows if _positive(item.get("size"))), None)
        positions[symbol.value] = {
            "size": str((active or {}).get("size") or "0"),
            "side": str((active or {}).get("side") or ""),
        }
        if active:
            active_position_symbols.add(symbol.value)

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
    return DemoDiagnosticsResult(
        kill_switch=kill_switch,
        latest_execution=latest,
        bot_owned_open_orders=bot_orders,
        unrelated_open_orders=unrelated_orders,
        positions=positions,
        unresolved_executions=unresolved,
        failures=failures,
    )


def format_demo_diagnostics(result: DemoDiagnosticsResult) -> str:
    latest = result.latest_execution
    reasons = result.kill_switch.get("reasons") or []
    lines = [
        f"DEMO KILL SWITCH ACTIVE: {str(bool(result.kill_switch.get('active'))).lower()}",
        "DEMO KILL SWITCH REASONS: " + ("; ".join(reasons) or "none"),
        "DEMO KILL SWITCH ACTIVATED AT: "
        + _display_time(result.kill_switch.get("activated_at")),
        "DEMO KILL SWITCH ACTIVATION COUNT: "
        + str(result.kill_switch.get("activation_count") or 0),
        "LATEST DEMO EXECUTION ID: " + (str(latest.id) if latest else "none"),
        "LATEST DEMO EXECUTION STATE: " + (latest.state.value if latest else "none"),
        "LATEST DEMO FAILURE REASON: " + ((latest.failure_reason or "none") if latest else "none"),
        "LATEST DEMO CLEANUP RESULT: " + ((latest.cleanup_result or "none") if latest else "none"),
        f"REMOTE BOT-OWNED OPEN ORDERS: {len(result.bot_owned_open_orders)}",
        f"REMOTE UNRELATED OPEN ORDERS: {len(result.unrelated_open_orders)}",
        "REMOTE BTCUSDT POSITION: " + json.dumps(result.positions["BTCUSDT"]),
        "REMOTE ETHUSDT POSITION: " + json.dumps(result.positions["ETHUSDT"]),
        "UNRESOLVED DURABLE EXECUTIONS: "
        + (", ".join(str(item.id) for item in result.unresolved_executions) or "none"),
        "READ-ONLY DIAGNOSTICS: " + ("PASS" if result.passed else "FAIL"),
    ]
    if result.failures:
        lines.append("FAILURES: " + "; ".join(result.failures))
    return "\n".join(lines)


def _execution_is_resolved(record: DemoExecutionRecord) -> bool:
    if record.state == DemoExecutionState.DEMO_CLOSED:
        return True
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


def _positive(value: object) -> bool:
    try:
        return float(str(value or "0")) > 0
    except ValueError:
        raise DemoDiagnosticsError("Bybit returned an invalid position size")


def _display_time(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value or "none")


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
