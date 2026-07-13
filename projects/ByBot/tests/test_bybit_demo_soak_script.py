from pathlib import Path
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "bybit_demo_soak.ps1"


def test_demo_soak_requires_explicit_human_confirmation() -> None:
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT), "-Hours", "0.001", "-SampleSeconds", "5",
        ],
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 1
    assert "AllowDemoOrders" in (result.stdout + result.stderr)


def test_demo_soak_uses_real_mode_and_has_no_test_pipeline() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'Set-IsolatedEnvironment "APP_ENV" "demo"' in text
    assert 'Set-IsolatedEnvironment "TEST_MODE" "false"' in text
    assert 'Set-IsolatedEnvironment "EXECUTION_MODE" "BYBIT_DEMO"' in text
    assert 'Set-IsolatedEnvironment "BYBIT_LIVE_TRADING_ENABLED" "false"' in text
    assert 'Set-IsolatedEnvironment "AUTO_PAPER_EXECUTION" "false"' in text
    assert "/signals/test-from-news" not in text
    assert "/test-market-snapshot" not in text
    assert "/paper/test/execute-candidate" not in text
    assert "/news/restore-status" in text
    assert "Assert-NoInvalidCurrentRunCandidates" in text


def test_demo_soak_waits_for_readiness_and_reports_real_startup_failure() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'Invoke-Api -Path "/health"' in text
    assert 'Invoke-Api -Path "/status"' in text
    assert "$script:Child.WaitForExit()" in text
    assert "Get-UvicornExitCode" in text
    assert '"FastAPI exit code: $exitText"' in text
    assert '"First exception line:' in text
    assert '"Final error line:' in text
    assert '{ "unknown" }' in text
    assert "if ($exitCode -eq 0) { return $null }" in text


def test_demo_soak_prints_sanitized_reconciliation_preflight() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'Invoke-Api -Method "POST" -Path "/demo/reconcile"' in text
    assert '"DEMO OPEN ORDERS ${symbol}: $count"' in text
    assert '"DEMO USDT ORDER RECONCILIATION: PASS"' in text
    assert '"DEMO USDT POSITION RECONCILIATION: PASS"' in text


def test_demo_soak_script_parses_in_windows_powershell() -> None:
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
