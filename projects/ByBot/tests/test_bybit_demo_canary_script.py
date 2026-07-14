from pathlib import Path
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "bybit_demo_canary.ps1"


def test_demo_canary_requires_explicit_human_confirmation() -> None:
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT),
        ],
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 1
    assert "AllowDemoOrders" in result.stdout + result.stderr


def test_demo_canary_rejects_notional_above_twenty_before_setup() -> None:
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT), "-AllowDemoOrders", "-NotionalUSDT", "20.01",
        ],
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 1
    assert "at most 20" in result.stdout + result.stderr


def test_demo_canary_uses_production_demo_service_contract() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'Set-IsolatedEnvironment "APP_ENV" "demo"' in text
    assert 'Set-IsolatedEnvironment "TEST_MODE" "false"' in text
    assert 'Set-IsolatedEnvironment "EXECUTION_MODE" "BYBIT_DEMO"' in text
    assert 'Set-IsolatedEnvironment "BYBIT_ENABLE_TRADING" "false"' in text
    assert 'Set-IsolatedEnvironment "BYBIT_LIVE_TRADING_ENABLED" "false"' in text
    assert 'Set-IsolatedEnvironment "DEMO_CANARY_ENABLED" "true"' in text
    assert '"https://api-demo.bybit.com"' in text
    assert 'Path "/demo/canary/execute"' in text
    assert 'Path "/demo/canary/$ExecutionId"' in text
    assert 'Path "/demo/canary/$executionId/close"' in text
    assert "/paper/" not in text
    assert "/signals/test" not in text
    assert "create_order" not in text


def test_demo_canary_enforces_maximum_notional_and_reconciliation() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "$NotionalUSDT -gt [decimal]20" in text
    assert 'Path "/demo/reconcile"' in text
    assert "Exactly one durable Demo execution was not created" in text
    assert "DEMO_POSITION_OPEN" in text
    assert "protection_confirmed" in text
    assert "accepted_quantity" in text
    assert "average_fill_price" in text
    assert "reduce_only" in text
    assert "DEMO_CLOSED" in text


def test_demo_canary_declares_required_pass_output() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for line in (
        "DEMO ACCOUNT VERIFIED: PASS",
        "DEMO ENTRY ACCEPTED: PASS",
        "DEMO FILL CONFIRMED: PASS",
        "DEMO TP/SL VERIFIED: PASS",
        "RESTART RECONCILIATION: PASS",
        "IDEMPOTENCY: PASS",
        "DEMO REDUCE-ONLY CLOSE: PASS",
        "FINAL DEMO STATE FLAT: PASS",
        "LIVE EXECUTION BLOCKED: PASS",
        "OVERALL: PASS",
    ):
        assert line in text


def test_demo_canary_script_parses_in_windows_powershell() -> None:
    command = (
        "$errors=$null; [void][System.Management.Automation.Language.Parser]::"
        f"ParseFile('{str(SCRIPT).replace(chr(39), chr(39) * 2)}',"
        "[ref]$null,[ref]$errors); if($errors.Count){$errors|%{$_.Message};exit 1}"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
