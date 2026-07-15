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


def test_demo_canary_rejects_non_positive_maximum_budget_before_setup() -> None:
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT), "-AllowDemoOrders", "-MaxNotionalUSDT", "0",
        ],
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 1
    assert "MaxNotionalUSDT" in result.stdout + result.stderr


def test_demo_canary_uses_production_demo_service_contract() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'Set-IsolatedEnvironment "APP_ENV" "demo"' in text
    assert 'Set-IsolatedEnvironment "TEST_MODE" "false"' in text
    assert 'Set-IsolatedEnvironment "EXECUTION_MODE" "BYBIT_DEMO"' in text
    assert 'Set-IsolatedEnvironment "BYBIT_ENABLE_TRADING" "false"' in text
    assert 'Set-IsolatedEnvironment "BYBIT_LIVE_TRADING_ENABLED" "false"' in text
    assert 'Set-IsolatedEnvironment "DEMO_ORDER_EXECUTION_AUTHORIZED" "true"' in text
    assert 'Set-IsolatedEnvironment "DEMO_CANARY_ENABLED" "true"' in text
    assert '"https://api-demo.bybit.com"' in text
    assert 'Path "/demo/canary/preview"' in text
    assert 'Path "/demo/canary/execute"' in text
    assert 'Path "/demo/canary/$ExecutionId"' in text
    assert 'Path "/demo/canary/$executionId/close"' in text
    assert 'Path "/demo/canary/$($script:ExecutionId)/failure-cleanup"' in text
    assert "/paper/" not in text
    assert "/signals/test" not in text
    assert "create_order" not in text


def test_demo_canary_enforces_maximum_notional_and_reconciliation() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "[Nullable[decimal]]$MaxNotionalUSDT = $null" in text
    assert "$null -eq $MaxNotionalUSDT" in text
    assert "buffered_required_notional" in text
    assert "calculated_quantity" in text
    assert "expected_rules_fingerprint" in text
    assert "requested_quantity" in text
    assert "\n        notional_usdt =" not in text
    assert 'Path "/demo/reconcile"' in text
    assert "Exactly one durable Demo execution was not created" in text
    assert "DEMO_POSITION_OPEN" in text
    assert "protection_confirmed" in text
    assert "accepted_quantity" in text
    assert "average_fill_price" in text
    assert "reduce_only" in text
    assert "DEMO_CLOSED" in text


def test_demo_canary_prints_exchange_minimum_plan_before_execution() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    preview_index = text.index('Path "/demo/canary/preview"')
    confirmation_index = text.index("$operatorConfirmation = Read-Host")
    execute_index = text.index('Path "/demo/canary/execute"')
    assert preview_index < confirmation_index < execute_index
    assert '"SUBMIT $Symbol $($plan.calculated_quantity)"' in text
    for label in (
        "DEMO SYMBOL:", "MIN ORDER QTY:", "QTY STEP:", "MIN NOTIONAL:",
        "REFERENCE PRICE:", "CALCULATED ORDER QTY:",
        "ESTIMATED NOTIONAL:", "BUFFERED REQUIRED BUDGET:",
        "MAX CANARY BUDGET:", "EXCHANGE MINIMUM VALIDATION: PASS",
    ):
        assert label in text


def test_demo_canary_declares_required_pass_output() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for line in (
        "DEMO ACCOUNT VERIFIED: PASS",
        "DEMO ORDER ACKNOWLEDGED: PASS",
        "DEMO ENTRY FILL CONFIRMED: PASS",
        "DEMO POSITION OPEN CONFIRMED: PASS",
        "DEMO TP/SL VERIFIED: PASS",
        "RESTART RECONCILIATION: PASS",
        "IDEMPOTENCY: PASS",
        "DEMO REDUCE-ONLY CLOSE: PASS",
        "FINAL DEMO STATE FLAT: PASS",
        "LIVE EXECUTION BLOCKED: PASS",
        "OVERALL: PASS",
    ):
        assert line in text


def test_demo_canary_reports_functional_and_cleanup_results_separately() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for line in (
        "DEMO ENTRY FILL CONFIRMED DURING CLEANUP: PASS",
        "DEMO REDUCE-ONLY CLEANUP CLOSE: PASS",
        "CANARY FUNCTIONAL RESULT: FAIL",
        "SAFETY CLEANUP RESULT:",
        "OVERALL: FAIL",
        "report.json",
    ):
        assert line in text
    finally_index = text.index("finally {")
    stop_index = text.index("Stop-Uvicorn", finally_index)
    assert stop_index > finally_index


def test_demo_canary_retries_transient_short_poll_timeouts() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'TimeoutSec 3' in text
    wait_start = text.index("function Wait-ForExecutionState")
    wait_end = text.index("\nfunction ", wait_start + 1)
    wait_body = text[wait_start:wait_end]
    assert 'Invoke-Api -Path "/health" -TimeoutSec 2' in wait_body
    assert "continue" in wait_body
    assert "FastAPI exited while polling" in wait_body


def test_demo_canary_cleanup_pass_requires_authoritative_flat_terminal_state() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    cleanup_index = text.index("DEMO REDUCE-ONLY CLEANUP CLOSE: PASS")
    condition_index = text.rfind("$ownCloseFilled", 0, cleanup_index)
    report_pass_index = text.find('$script:SafetyCleanupResult = "PASS"', cleanup_index)
    assert condition_index >= 0
    assert "$terminalCleanup" in text[condition_index:cleanup_index]
    assert "bot_owned_open_positions" in text[condition_index:cleanup_index]
    assert "bot_owned_open_orders" in text[condition_index:cleanup_index]
    assert report_pass_index > cleanup_index


def test_demo_canary_early_failure_report_has_no_side_effect_fields() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for field in (
        "started_at", "failure_stage", "failure_reason",
        "kill_switch_active", "kill_switch_reasons",
        "no_candidate_created", "no_reservation_created", "no_order_submitted",
    ):
        assert field in text
    assert "$script:NoCandidateCreated = $true" in text
    assert "$script:NoReservationCreated = $true" in text
    assert "$script:NoOrderSubmitted = $true" in text
    assert "if (-not $script:ReportWritten)" in text


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
