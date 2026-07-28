from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bybit_demo_canary.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def _prefix() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    return source.split("if ($ValidateLotGuardOnly)", 1)[0]


def _run(tmp_path: Path, body: str, *, timeout: int = 20):
    if POWERSHELL is None:
        pytest.skip("Windows PowerShell 5.1 is unavailable")
    harness = tmp_path / "controller-harness.ps1"
    harness.write_text(_prefix() + "\n" + body + "\n", encoding="utf-8")
    return subprocess.run(
        [
            POWERSHELL,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _child(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="ascii")
    return path


def _quoted(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def test_controller_healthy_bounded_step_completes(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "Invoke-ControllerStep 'healthy_preflight' 5 { "
        "Start-Sleep -Milliseconds 50; 'PASS' }",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


def test_controller_native_timeout_names_exact_stage(tmp_path: Path) -> None:
    child = _child(tmp_path, "timeout.ps1", "Start-Sleep -Seconds 5\n")
    result = _run(
        tmp_path,
        "Invoke-NativeCommand "
        f"{_quoted(POWERSHELL or '')} "
        f"@('-NoProfile','-File',{_quoted(child)}) "
        "'CANARY PREFLIGHT TEST' 1",
    )
    assert result.returncode != 0
    assert "CANARY PREFLIGHT TEST timed out after 1 seconds" in (
        result.stdout + result.stderr
    )


def test_controller_redirected_stdout_stderr_do_not_deadlock(tmp_path: Path) -> None:
    child = _child(
        tmp_path,
        "output.ps1",
        "1..300 | % { Write-Output ('out-' + $_); "
        "[Console]::Error.WriteLine('err-' + $_) }\nexit 0\n",
    )
    result = _run(
        tmp_path,
        "Invoke-NativeCommand "
        f"{_quoted(POWERSHELL or '')} "
        f"@('-NoProfile','-File',{_quoted(child)}) 'OUTPUT TEST' 10",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "out-300" in result.stdout
    assert "err-300" in result.stdout


def test_controller_child_near_timeout_boundary_completes(tmp_path: Path) -> None:
    child = _child(
        tmp_path, "boundary.ps1",
        "Start-Sleep -Milliseconds 700\nWrite-Output 'boundary-pass'\nexit 0\n",
    )
    result = _run(
        tmp_path,
        "Invoke-NativeCommand "
        f"{_quoted(POWERSHELL or '')} "
        f"@('-NoProfile','-File',{_quoted(child)}) 'BOUNDARY TEST' 3",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "boundary-pass" in result.stdout


def test_controller_timeout_uses_exact_process_tree_cleanup() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    native = source.split("function Invoke-NativeCommand", 1)[1].split(
        "function Get-AvailablePort", 1
    )[0]
    stop = source.split("function Stop-Uvicorn", 1)[1].split(
        "function Start-Uvicorn", 1
    )[0]
    assert "/PID $process.Id /T /F" in native
    assert "/PID $rootPid /T /F" in stop
    assert "$process.WaitForExit()" in native
    assert "$process.Refresh()" in native


def test_controller_cleanup_releases_canary_port_by_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    finally_body = source.split("finally {", 1)[1]
    assert "Stop-Uvicorn" in finally_body
    assert "$script:Child.Dispose()" in source


def test_candidate_creation_occurs_only_after_all_preflight_gates() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.index('"local_demo_preflight"') < source.index(
        'Path "/demo/canary/preview"'
    )
    assert source.index('"v2_execution_preflight"') < source.index(
        'Path "/v2/canary/sizing/$Symbol/$V2SizingTier"'
    )


def test_preflight_timeout_has_no_exchange_mutation_path() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    catch_body = source.split("catch {", 1)[1]
    assert "if ($script:ExecutionId" in source
    assert "$script:ExecutionId = $null" in source
    assert "$script:NoOrderSubmitted = $true" in source
    assert "failure-cleanup" in catch_body


def test_one_authorization_has_one_real_entry_attempt() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.count('Path "/v2/canary/sizing/$Symbol/$V2SizingTier"') == 1
    assert source.count('Path "/demo/canary/execute"') == 1
    assert "for ($attempt" not in source[
        source.index('Path "/v2/canary/sizing/$Symbol/$V2SizingTier"'):
        source.index("$script:ExecutionId = [string]$entry.execution.id")
    ]


def test_controller_script_parses_in_windows_powershell_51() -> None:
    if POWERSHELL is None:
        pytest.skip("Windows PowerShell 5.1 is unavailable")
    command = (
        "$errors=$null; [void][System.Management.Automation.Language.Parser]::"
        f"ParseFile('{str(SCRIPT).replace(chr(39), chr(39) * 2)}',"
        "[ref]$null,[ref]$errors); "
        "if($errors.Count){$errors|%{$_.Message};exit 1}"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
