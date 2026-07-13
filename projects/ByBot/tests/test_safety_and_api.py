import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.main as main_module
from app.bybit.market_data import build_market_data_service
from app.bybit.private import BybitAccountService, BybitPrivateClient, build_account_service
from app.config import BybitEnvironment, MarketDataProviderName, Settings
from app.models import PaperTestSignalRequest, Side, Symbol
from app.portfolio.paper_trading import PaperTradingService


main_module.market_data_service = build_market_data_service(
    Settings(market_data_provider=MarketDataProviderName.MOCK)
)
main_module.account_service = build_account_service(
    Settings(bybit_api_key=None, bybit_api_secret=None)
)
main_module.paper_trading_service = PaperTradingService()


def test_live_mode_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Live trading is blocked"):
        Settings(bot_mode="LIVE")


def test_unsupported_symbols_are_rejected() -> None:
    with pytest.raises(ValidationError, match="Unsupported symbols"):
        Settings(allowed_symbols=("SOLUSDT",))


def test_bybit_enable_trading_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Bybit order placement is blocked"):
        Settings(bybit_enable_trading=True)


def test_auto_paper_execution_is_disabled_by_default_but_can_be_enabled() -> None:
    assert Settings(_env_file=None).auto_paper_execution is False
    assert Settings(_env_file=None, auto_paper_execution=True).auto_paper_execution is True
    with pytest.raises(ValidationError, match="Bybit order placement is blocked"):
        Settings(_env_file=None, auto_paper_execution=True, bybit_enable_trading=True)


def test_paper_test_execution_endpoints_are_hidden_outside_local_test_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "settings",
        Settings(_env_file=None, app_env="production", test_mode=False),
    )

    with pytest.raises(HTTPException) as exc:
        main_module.paper_test_execute_candidate(
            "00000000-0000-0000-0000-000000000000"
        )

    assert exc.value.status_code == 404


def test_health_and_status_report_live_disabled() -> None:
    assert main_module.health()["status"] == "ok"
    payload = main_module.status()
    assert payload["live_trading"] is False
    assert payload["mode"] in {"DATA_ONLY", "PAPER", "BYBIT_DEMO"}
    assert isinstance(payload["trading_enabled"], bool)
    assert payload["trading_paused"] is False
    assert payload["active_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert payload["open_paper_position"] is None
    assert payload["last_signal"] is None or payload["last_signal"]["action"] in {"TRADE", "NO_TRADE"}
    assert payload["market"]["status"] == "OK"
    assert payload["market_data_status"] == "OK"
    assert payload["latest_btcusdt_snapshot"] is not None
    assert payload["latest_ethusdt_snapshot"] is not None
    assert payload["trading_blocked_data_unavailable"] is False
    assert payload["private_api_connected"] is False
    assert payload["order_placement_blocked"] is True
    assert payload["account"]["trading_enabled"] is False
    assert payload["paper_trading_status"] in {"IDLE", "OPEN_POSITION"}
    assert payload["risk_status"]["state"] in {"OK", "BLOCKED"}


def test_market_endpoint_returns_snapshots() -> None:
    payload = main_module.market()

    assert payload["status"] == "OK"
    assert {item["symbol"] for item in payload["snapshots"]} == {"BTCUSDT", "ETHUSDT"}
    assert {
        "last_price",
        "bid_price",
        "ask_price",
        "spread",
        "spread_pct",
        "price_change_1m_pct",
        "simple_trend",
        "simple_volatility",
    } <= set(payload["snapshots"][0])


def test_market_symbol_endpoint_returns_single_snapshot() -> None:
    payload = main_module.market_symbol("BTCUSDT")

    assert payload["status"] == "OK"
    assert payload["snapshot"]["symbol"] == "BTCUSDT"


def test_market_symbol_endpoint_rejects_unsupported_symbol() -> None:
    with pytest.raises(HTTPException) as exc:
        main_module.market_symbol("SOLUSDT")
    assert exc.value.status_code == 404


def test_account_endpoint_returns_safe_disconnected_status() -> None:
    payload = main_module.account()

    assert payload["connected"] is False
    assert payload["environment"] == "demo"
    assert payload["trading_enabled"] is False


def test_account_endpoint_and_status_show_connected_after_refresh() -> None:
    def fake_http_get(url: str, headers: dict[str, str], timeout: float) -> dict[str, object]:
        if "/v5/account/wallet-balance" in url:
            return {
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "list": [
                        {
                            "totalEquity": "1234",
                            "totalAvailableBalance": "1200",
                            "coin": [{"coin": "USDT", "walletBalance": "1200"}],
                        }
                    ]
                },
            }
        return {"retCode": 0, "retMsg": "OK", "result": {"list": []}}

    previous_account_service = main_module.account_service
    try:
        main_module.account_service = BybitAccountService(
            BybitPrivateClient(
                api_key="fake_key",
                api_secret="fake_secret",
                environment=BybitEnvironment.DEMO,
                demo_base_url="https://api-demo.bybit.com",
                mainnet_base_url="https://api.bybit.com",
                http_get=fake_http_get,
            ),
            (Symbol.BTCUSDT, Symbol.ETHUSDT),
        )

        account_payload = main_module.account()
        status_payload = main_module.status()

        assert account_payload["connected"] is True
        assert status_payload["private_api_connected"] is True
        assert status_payload["account_connection_status"] == "CONNECTED"
        assert status_payload["account"]["equity"] == 1234
        assert "private API is not connected" not in status_payload["risk_status"]["reasons"]
    finally:
        main_module.account_service = previous_account_service


def test_paper_test_signal_and_state_endpoints() -> None:
    main_module.paper_trading_service = PaperTradingService()

    response = main_module.paper_test_signal(
        PaperTestSignalRequest(symbol=Symbol.BTCUSDT, side=Side.BUY)
    )

    assert response["accepted"] is True
    assert main_module.paper_positions()["positions"]
    assert "realized_pnl" in main_module.paper_pnl()
    assert "trades" in main_module.paper_trades()
    assert main_module.status()["order_placement_blocked"] is True


def test_paper_close_position_endpoint_moves_open_position_to_trades() -> None:
    main_module.paper_trading_service = PaperTradingService()

    opened = main_module.paper_test_signal(
        PaperTestSignalRequest(symbol=Symbol.BTCUSDT, side=Side.BUY)
    )
    closed = main_module.paper_close_position()

    assert opened["accepted"] is True
    assert closed["closed"] is True
    assert closed["position"]["reason"] == "manual_close"
    assert main_module.paper_positions()["positions"] == []
    trades = main_module.paper_trades()["trades"]
    assert len(trades) == 1
    assert trades[0]["status"] == "CLOSED"
    assert trades[0]["reason"] == "manual_close"
    pnl = main_module.paper_pnl()
    assert pnl["closed_trades"] == 1
    assert pnl["unrealized_pnl"] == 0
    assert set(pnl) == {
        "starting_equity",
        "equity",
        "realized_pnl",
        "unrealized_pnl",
        "total_pnl",
        "fees_paid",
        "open_positions",
        "closed_trades",
    }
    assert pnl["equity"] == pytest.approx(
        pnl["starting_equity"] + pnl["realized_pnl"] + pnl["unrealized_pnl"]
    )
    status = main_module.status()
    for field in (
        "paper_starting_equity_usdt",
        "paper_equity",
        "paper_realized_pnl",
        "paper_unrealized_pnl",
        "paper_fees_paid",
    ):
        assert field in status


def test_paper_test_signal_blocks_second_position() -> None:
    main_module.paper_trading_service = PaperTradingService()

    first = main_module.paper_test_signal(
        PaperTestSignalRequest(symbol=Symbol.BTCUSDT, side=Side.BUY)
    )
    second = main_module.paper_test_signal(
        PaperTestSignalRequest(symbol=Symbol.BTCUSDT, side=Side.BUY)
    )

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["risk_decision"]["approved"] is False
