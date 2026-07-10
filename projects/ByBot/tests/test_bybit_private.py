from __future__ import annotations

from app.bybit.private import BybitAccountService, BybitPrivateClient, order_placement_blocked_reason
from app.config import BybitEnvironment, Settings
from app.models import Symbol


def test_private_client_signs_read_only_get_requests() -> None:
    captured: dict[str, object] = {}

    def fake_http_get(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return {"retCode": 0, "retMsg": "OK", "result": {"list": []}}

    client = BybitPrivateClient(
        api_key="fake_key",
        api_secret="fake_secret",
        environment=BybitEnvironment.DEMO,
        demo_base_url="https://api-demo.bybit.com",
        mainnet_base_url="https://api.bybit.com",
        http_get=fake_http_get,
    )

    assert client.validate_api_key() is True
    assert "/v5/account/wallet-balance" in str(captured["url"])
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["X-BAPI-API-KEY"] == "fake_key"
    assert headers["X-BAPI-SIGN"]
    assert client.trading_enabled is False
    assert not hasattr(client, "place_order")


def test_account_service_parses_wallet_positions_and_orders() -> None:
    def fake_http_get(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        if "/v5/account/wallet-balance" in url:
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        {
                            "totalEquity": "1000.5",
                            "totalAvailableBalance": "900.25",
                            "coin": [{"coin": "USDT", "walletBalance": "900.25"}],
                        }
                    ]
                },
            }
        if "/v5/position/list" in url:
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        {
                            "symbol": "BTCUSDT",
                            "side": "Buy",
                            "size": "0.01",
                            "avgPrice": "60000",
                            "markPrice": "60100",
                            "unrealisedPnl": "1.0",
                        }
                    ]
                },
            }
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "symbol": "ETHUSDT",
                        "orderId": "abc",
                        "side": "Sell",
                        "orderType": "Limit",
                        "qty": "0.1",
                        "price": "3000",
                        "orderStatus": "New",
                    }
                ]
            },
        }

    client = BybitPrivateClient(
        api_key="fake_key",
        api_secret="fake_secret",
        environment=BybitEnvironment.DEMO,
        demo_base_url="https://api-demo.bybit.com",
        mainnet_base_url="https://api.bybit.com",
        http_get=fake_http_get,
    )
    service = BybitAccountService(client, (Symbol.BTCUSDT, Symbol.ETHUSDT))

    status = service.refresh()

    assert status.connected is True
    assert status.environment == "demo"
    assert status.trading_enabled is False
    assert status.equity == 1000.5
    assert status.available_balance == 900.25
    assert status.open_positions[0].symbol == Symbol.BTCUSDT
    assert status.open_orders[0].order_id == "abc"


def test_account_service_stays_safe_on_private_api_error() -> None:
    def fake_http_get(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        return {"retCode": 10003, "retMsg": "invalid api key", "result": {}}

    client = BybitPrivateClient(
        api_key="fake_key",
        api_secret="fake_secret",
        environment=BybitEnvironment.DEMO,
        demo_base_url="https://api-demo.bybit.com",
        mainnet_base_url="https://api.bybit.com",
        http_get=fake_http_get,
    )
    service = BybitAccountService(client, (Symbol.BTCUSDT,))

    status = service.refresh()

    assert status.connected is False
    assert status.trading_enabled is False
    assert "invalid api key" in str(status.last_error)


def test_order_placement_is_always_blocked_in_phase_3a() -> None:
    settings = Settings()
    service = BybitAccountService(
        BybitPrivateClient(
            api_key="fake_key",
            api_secret="fake_secret",
            environment=BybitEnvironment.DEMO,
            demo_base_url="https://api-demo.bybit.com",
            mainnet_base_url="https://api.bybit.com",
            http_get=lambda url, headers, timeout: {
                "retCode": 0,
                "retMsg": "OK",
                "result": {"list": []},
            },
        ),
        (Symbol.BTCUSDT,),
    )

    assert order_placement_blocked_reason(settings, service.status)
