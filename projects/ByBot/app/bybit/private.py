from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import BybitEnvironment, Settings
from app.models import AccountOrder, AccountPosition, AccountStatus, Symbol


PrivateHttpGet = Callable[[str, dict[str, str], float], dict[str, Any]]


class BybitPrivateDataProvider(Protocol):
    environment: str
    trading_enabled: bool

    def get_account_status(self) -> AccountStatus: ...


def _default_private_http_get(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Bybit returned a non-object response")
    return data


class BybitPrivateClient:
    """Read-only Bybit V5 private client.

    This class only performs signed GET requests. It intentionally has no
    create, amend, cancel, or trading-stop methods.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        environment: BybitEnvironment,
        demo_base_url: str,
        mainnet_base_url: str,
        recv_window_ms: int = 5000,
        timeout_seconds: float = 5.0,
        http_get: PrivateHttpGet | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.environment = environment.value
        self.base_url = (
            demo_base_url if environment == BybitEnvironment.DEMO else mainnet_base_url
        ).rstrip("/")
        self.recv_window_ms = recv_window_ms
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get or _default_private_http_get
        self.trading_enabled = False

    def validate_api_key(self) -> bool:
        self.get_wallet_balance()
        return True

    def get_wallet_balance(self) -> dict[str, Any]:
        return self._signed_get(
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": "USDT"},
        )

    def get_open_positions(self, symbols: tuple[Symbol, ...]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for symbol in symbols:
            data = self._signed_get(
                "/v5/position/list",
                {"category": "linear", "symbol": symbol.value},
            )
            items.extend(_extract_list(data))
        return items

    def get_open_orders(self, symbols: tuple[Symbol, ...]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for symbol in symbols:
            data = self._signed_get(
                "/v5/order/realtime",
                {"category": "linear", "symbol": symbol.value, "openOnly": "0"},
            )
            items.extend(_extract_list(data))
        return items

    def get_recent_closed_orders(self, symbols: tuple[Symbol, ...]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for symbol in symbols:
            data = self._signed_get(
                "/v5/order/realtime",
                {"category": "linear", "symbol": symbol.value, "openOnly": "1"},
            )
            items.extend(_extract_list(data))
        return items

    def _signed_get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        query = urlencode(sorted(params.items()))
        timestamp = str(int(time.time() * 1000))
        recv_window = str(self.recv_window_ms)
        payload = f"{timestamp}{self.api_key}{recv_window}{query}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "User-Agent": "ByBot/0.3 READ_ONLY",
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }
        data = self.http_get(f"{self.base_url}{path}?{query}", headers, self.timeout_seconds)
        if data.get("retCode") != 0:
            message = data.get("retMsg", "unknown Bybit error")
            raise ValueError(f"Bybit private request failed: {message}")
        return data


class MockBybitPrivateClient:
    trading_enabled = False

    def __init__(self, environment: str = "demo") -> None:
        self.environment = environment

    def get_account_status(self) -> AccountStatus:
        return AccountStatus(
            connected=False,
            environment=self.environment,
            trading_enabled=False,
            last_error="private API not configured",
        )


class BybitAccountService:
    def __init__(
        self,
        client: BybitPrivateClient | MockBybitPrivateClient,
        symbols: tuple[Symbol, ...],
        *,
        refresh_interval: timedelta = timedelta(seconds=30),
    ) -> None:
        self.client = client
        self.symbols = symbols
        self.refresh_interval = refresh_interval
        self.status = AccountStatus(
            connected=False,
            environment=client.environment,
            trading_enabled=False,
            last_error="not refreshed",
        )

    def refresh(self) -> AccountStatus:
        now = datetime.now(timezone.utc)
        if isinstance(self.client, MockBybitPrivateClient):
            self.status = self.client.get_account_status()
            return self.status

        try:
            wallet = self.client.get_wallet_balance()
            positions = self.client.get_open_positions(self.symbols)
            open_orders = self.client.get_open_orders(self.symbols)
            closed_orders = self.client.get_recent_closed_orders(self.symbols)
            equity, available_balance = _parse_wallet(wallet)
            self.status = AccountStatus(
                connected=True,
                environment=self.client.environment,
                trading_enabled=False,
                equity=equity,
                available_balance=available_balance,
                open_positions=[_parse_position(item) for item in positions],
                open_orders=[_parse_order(item) for item in open_orders],
                recent_closed_orders=[_parse_order(item) for item in closed_orders],
                stale=False,
                last_error=None,
                updated_at=now,
                last_refresh_attempt_at=now,
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            message = str(exc)
            if self.status.connected:
                self.status = self.status.model_copy(
                    update={
                        "stale": True,
                        "last_error": message,
                        "last_refresh_attempt_at": now,
                    }
                )
            else:
                self.status = AccountStatus(
                    connected=False,
                    environment=self.client.environment,
                    trading_enabled=False,
                    stale=False,
                    last_error=message,
                    updated_at=now,
                    last_refresh_attempt_at=now,
                )
        return self.status

    def refresh_if_stale(self, *, force: bool = False) -> AccountStatus:
        if isinstance(self.client, MockBybitPrivateClient):
            return self.status if self.status.last_error != "not refreshed" else self.refresh()

        now = datetime.now(timezone.utc)
        last_attempt = self.status.last_refresh_attempt_at
        never_refreshed = last_attempt is None or self.status.last_error == "not refreshed"
        refresh_due = (
            last_attempt is None
            or now - last_attempt >= self.refresh_interval
            or self.status.stale
        )
        if force or never_refreshed or refresh_due:
            return self.refresh()
        return self.status

    def as_payload(self) -> dict[str, Any]:
        return self.status.model_dump(mode="json")


def build_account_service(settings: Settings) -> BybitAccountService:
    symbols = tuple(Symbol(symbol) for symbol in settings.allowed_symbols)
    refresh_interval = timedelta(seconds=settings.bybit_account_refresh_interval_seconds)
    if (
        not settings.bybit_api_key
        or not settings.bybit_api_secret
        or _is_fake_placeholder(settings.bybit_api_key)
        or _is_fake_placeholder(settings.bybit_api_secret)
    ):
        return BybitAccountService(
            MockBybitPrivateClient(settings.bybit_env.value),
            symbols,
            refresh_interval=refresh_interval,
        )

    client = BybitPrivateClient(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
        environment=settings.bybit_env,
        demo_base_url=settings.bybit_private_demo_base_url,
        mainnet_base_url=settings.bybit_private_mainnet_base_url,
        recv_window_ms=settings.bybit_private_recv_window_ms,
        timeout_seconds=settings.bybit_private_timeout_seconds,
    )
    return BybitAccountService(client, symbols, refresh_interval=refresh_interval)


def order_placement_blocked_reason(settings: Settings, account: AccountStatus) -> str:
    if not settings.bybit_enable_trading:
        return "BYBIT_ENABLE_TRADING is false"
    if settings.bybit_env != BybitEnvironment.DEMO:
        return "Bybit environment is not demo"
    if not account.connected:
        return "Private API is not connected"
    return "Order placement is blocked in Phase 3A"


def _extract_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    items = result.get("list")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _parse_wallet(data: dict[str, Any]) -> tuple[float | None, float | None]:
    accounts = _extract_list(data)
    if not accounts:
        return None, None
    account = accounts[0]
    equity = _optional_float(account.get("totalEquity"))
    available = _optional_float(account.get("totalAvailableBalance"))
    coins = account.get("coin")
    if available is None and isinstance(coins, list):
        for coin in coins:
            if isinstance(coin, dict) and coin.get("coin") == "USDT":
                available = _optional_float(coin.get("walletBalance"))
                break
    return equity, available


def _parse_position(item: dict[str, Any]) -> AccountPosition:
    symbol = Symbol(str(item.get("symbol", "")).upper())
    return AccountPosition(
        symbol=symbol,
        side=str(item.get("side") or "None"),
        size=_optional_float(item.get("size")) or 0.0,
        entry_price=_optional_float(item.get("avgPrice")),
        mark_price=_optional_float(item.get("markPrice")),
        unrealized_pnl=_optional_float(item.get("unrealisedPnl")),
    )


def _parse_order(item: dict[str, Any]) -> AccountOrder:
    symbol = Symbol(str(item.get("symbol", "")).upper())
    return AccountOrder(
        symbol=symbol,
        order_id=str(item.get("orderId") or ""),
        side=item.get("side"),
        order_type=item.get("orderType"),
        qty=_optional_float(item.get("qty")),
        price=_optional_float(item.get("price")),
        order_status=item.get("orderStatus"),
        created_time=item.get("createdTime"),
    )


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_fake_placeholder(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("fake_") or "do_not_use" in lowered
