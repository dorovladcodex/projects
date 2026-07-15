from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.bybit.demo import (
    DemoExecutionService,
    DemoSafetyError,
    validate_demo_order_execution_enabled,
)
from app.config import Settings


def _authorized_demo_settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "app_env": "demo",
        "test_mode": False,
        "bot_mode": "BYBIT_DEMO",
        "execution_mode": "BYBIT_DEMO",
        "bybit_env": "demo",
        "bybit_api_key": "fake-demo-key",
        "bybit_api_secret": "fake-demo-secret",
        "bybit_demo_trading_enabled": True,
        "demo_order_execution_authorized": True,
        "bybit_enable_trading": False,
        "bybit_live_trading_enabled": False,
        "auto_paper_execution": False,
    }
    values.update(updates)
    return Settings(**values)


def test_settings_accepts_demo_mode_with_automatic_trading_disabled() -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        test_mode=True,
        bot_mode="PAPER",
        execution_mode="BYBIT_DEMO",
        bybit_demo_trading_enabled=False,
        demo_order_execution_authorized=False,
    )

    assert settings.bybit_demo_trading_enabled is False
    assert settings.demo_order_execution_authorized is False


def test_app_main_imports_with_demo_trading_disabled_in_clean_subprocess() -> None:
    env = os.environ.copy()
    env.update({
        "APP_ENV": "test",
        "BOT_MODE": "PAPER",
        "EXECUTION_MODE": "BYBIT_DEMO",
        "TEST_MODE": "true",
        "BYBIT_ENV": "demo",
        "BYBIT_ENABLE_TRADING": "false",
        "BYBIT_DEMO_TRADING_ENABLED": "false",
        "DEMO_ORDER_EXECUTION_AUTHORIZED": "false",
        "BYBIT_LIVE_TRADING_ENABLED": "false",
        "DEMO_CANARY_ENABLED": "false",
        "V2_ENABLED": "false",
        "V2_AUTO_DEMO_EXECUTION": "false",
        "AUTO_PAPER_EXECUTION": "false",
        "MARKET_DATA_PROVIDER": "MOCK",
        "NEWS_CLASSIFIER_MODE": "mock",
        "NEWS_ENABLE_RSS": "false",
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
    })
    env.pop("BYBIT_API_KEY", None)
    env.pop("BYBIT_API_SECRET", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app.main as module; "
                "assert module.settings.bybit_demo_trading_enabled is False; "
                "assert module.demo_execution_service.enabled is False"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_read_only_v2_preflight_modules_initialize_with_trading_disabled() -> None:
    preflight = importlib.import_module("scripts.demo_v2_preflight")
    settings = Settings(
        _env_file=None,
        app_env="test",
        execution_mode="BYBIT_DEMO",
        bybit_demo_trading_enabled=False,
        v2_enabled=True,
        allowed_symbols=("BTCUSDT", "ETHUSDT"),
    )
    service = DemoExecutionService(settings, object(), None, run_id="read-only-test")

    assert callable(preflight.main)
    assert service.enabled is False


def test_demo_mutation_guard_refuses_disabled_flag() -> None:
    settings = Settings(
        _env_file=None,
        execution_mode="BYBIT_DEMO",
        bybit_demo_trading_enabled=False,
    )

    with pytest.raises(DemoSafetyError, match="not explicitly enabled"):
        validate_demo_order_execution_enabled(settings)


def test_demo_mutation_guard_refuses_missing_explicit_authorization() -> None:
    settings = _authorized_demo_settings(demo_order_execution_authorized=False)

    with pytest.raises(DemoSafetyError, match="explicit Demo order authorization"):
        validate_demo_order_execution_enabled(settings)


def test_demo_mutation_guard_accepts_only_complete_demo_configuration() -> None:
    validate_demo_order_execution_enabled(_authorized_demo_settings())


def test_live_mainnet_and_testnet_execution_remain_impossible() -> None:
    with pytest.raises(ValidationError, match="permanently unavailable"):
        _authorized_demo_settings(bybit_live_trading_enabled=True)
    with pytest.raises(ValidationError, match="BYBIT_ENV must be demo"):
        _authorized_demo_settings(bybit_env="mainnet")
    with pytest.raises(ValidationError):
        _authorized_demo_settings(bybit_env="testnet")


def test_pytest_process_uses_fail_closed_environment_not_shell_values() -> None:
    assert os.environ["APP_ENV"] == "test"
    assert os.environ["BOT_MODE"] == "PAPER"
    assert os.environ["EXECUTION_MODE"] == "PAPER"
    assert os.environ["BYBIT_DEMO_TRADING_ENABLED"] == "false"
    assert os.environ["DEMO_ORDER_EXECUTION_AUTHORIZED"] == "false"
    assert os.environ["BYBIT_LIVE_TRADING_ENABLED"] == "false"

