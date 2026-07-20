from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "demo_v2_soak.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def _quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_helper_harness(tmp_path: Path, body: str, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    if POWERSHELL is None:
        pytest.skip("Windows PowerShell is unavailable")
    source = RUNNER.read_text(encoding="utf-8")
    helpers = source.split("if (-not $AllowDemoOrders)", maxsplit=1)[0]
    harness = tmp_path / "helper-harness.ps1"
    harness.write_text(helpers + "\n" + body + "\n", encoding="utf-8")
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


def _child_script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="ascii")
    return path


def _combined(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_native_command_captures_success_exit_code_and_stdout(tmp_path: Path) -> None:
    child = _child_script(tmp_path, "stdout.ps1", "Write-Output 'stdout-ok'\nexit 0\n")
    result = _run_helper_harness(
        tmp_path,
        f"Invoke-NativeCommand -FilePath {_quote(POWERSHELL or '')} "
        f"-Arguments @('-NoProfile','-File',{_quote(child)}) -Stage 'SUCCESS TEST'",
    )
    assert result.returncode == 0, _combined(result)
    assert "stdout-ok" in result.stdout


def test_native_command_preserves_redirected_stderr_on_success(tmp_path: Path) -> None:
    child = _child_script(
        tmp_path,
        "stderr.ps1",
        "[Console]::Error.WriteLine('stderr-ok')\nexit 0\n",
    )
    result = _run_helper_harness(
        tmp_path,
        f"Invoke-NativeCommand -FilePath {_quote(POWERSHELL or '')} "
        f"-Arguments @('-NoProfile','-File',{_quote(child)}) -Stage 'STDERR TEST'",
    )
    assert result.returncode == 0, _combined(result)
    assert "stderr-ok" in result.stdout


def test_native_command_reports_numeric_nonzero_exit_code(tmp_path: Path) -> None:
    child = _child_script(tmp_path, "failure.ps1", "exit 7\n")
    result = _run_helper_harness(
        tmp_path,
        f"Invoke-NativeCommand -FilePath {_quote(POWERSHELL or '')} "
        f"-Arguments @('-NoProfile','-File',{_quote(child)}) -Stage 'FAILURE TEST'",
    )
    assert result.returncode != 0
    assert "FAILURE TEST failed with exit code 7" in _combined(result)


def test_native_command_times_out_and_terminates_process_tree(tmp_path: Path) -> None:
    child = _child_script(tmp_path, "timeout.ps1", "Start-Sleep -Seconds 10\nexit 0\n")
    result = _run_helper_harness(
        tmp_path,
        f"Invoke-NativeCommand -FilePath {_quote(POWERSHELL or '')} "
        f"-Arguments @('-NoProfile','-File',{_quote(child)}) "
        "-TimeoutSeconds 1 -Stage 'TIMEOUT TEST'",
    )
    assert result.returncode != 0
    assert "TIMEOUT TEST timed out after 1 seconds" in _combined(result)


def test_unavailable_exit_code_has_explicit_defensive_branch() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert source.count("if ($null -eq $code)") >= 2
    assert "Native command exit code was not captured: stage=$Stage" in source
    assert "Native command exit code was not captured: stage=POSTGRESQL PORT" in source


def test_postgres_port_helper_accepts_exit_code_zero(tmp_path: Path) -> None:
    fake_docker = _child_script(
        tmp_path,
        "fake-docker.ps1",
        "Write-Output '127.0.0.1:6543'\nexit 0\n",
    )
    result = _run_helper_harness(
        tmp_path,
        "$port = Get-HostPostgresPort -Service 'db' "
        f"-DockerExecutable {_quote(POWERSHELL or '')} "
        f"-DockerPrefixArguments @('-NoProfile','-File',{_quote(fake_docker)})\n"
        "Write-Output ('PORT=' + $port)",
    )
    assert result.returncode == 0, _combined(result)
    assert "PORT=6543" in result.stdout


def test_both_helpers_finalize_and_refresh_before_exit_code() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    invoke_body = source.split("function Invoke-NativeCommand", 1)[1].split(
        "function Get-HostPostgresPort", 1
    )[0]
    port_body = source.split("function Get-HostPostgresPort", 1)[1].split(
        "function Set-HostDatabaseEnvironment", 1
    )[0]
    for body in (invoke_body, port_body):
        final_wait = body.index("$p.WaitForExit()", body.index("if (-not $p.WaitForExit"))
        refresh = body.index("$p.Refresh()", final_wait)
        exit_code = body.index("$p.ExitCode", refresh)
        dispose = body.rindex("$p.Dispose()")
        assert final_wait < refresh < exit_code < dispose


def test_runner_requires_final_read_only_diagnostics_for_safety_pass() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert "scripts\\demo_kill_switch_diagnostics.py" in source
    assert "FINAL READ-ONLY DIAGNOSTICS" in source
    assert "SAFETY RESULT: ' + $safetyResult" in source
    assert "unresolved durable execution or remote-state disagreement" in source
