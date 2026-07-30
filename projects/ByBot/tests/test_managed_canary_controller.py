from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "scripts" / "demo_v2_managed_canary_controller.py"
LAUNCHER = ROOT / "scripts" / "demo_v2_managed_protection_freshness_canary.ps1"
CANARY = ROOT / "scripts" / "bybit_demo_canary.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def _module():
    spec = importlib.util.spec_from_file_location("managed_canary_controller", CONTROLLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_managed_launcher_returns_after_persisted_heartbeat_contract() -> None:
    source = LAUNCHER.read_text(encoding="utf-8")
    assert "Start-Process -FilePath $python" in source
    assert "managed-canary.json" in source
    assert "heartbeat.json" in source
    assert "$controller.WaitForExit($HardTimeoutSeconds" not in source
    assert "$controller.WaitForExit()" in source  # only after HasExited
    assert "[int]$metadata.runner_pid -gt 0" in source


def test_managed_controller_has_explicit_hard_deadline_and_safe_gate() -> None:
    source = CONTROLLER.read_text(encoding="utf-8")
    assert '"hard_deadline"' in source
    assert "authoritative_flat_and_resolved()" in source
    assert "HARD_DEADLINE_SAFE_FLAT_TERMINATION" in source
    assert "HARD_DEADLINE_SAFETY_BLOCKED_PROCESS_RETAINED" in source


def test_canary_accepts_one_launcher_assigned_safe_run_id() -> None:
    source = CANARY.read_text(encoding="utf-8")
    assert "[string]$RunId" in source
    assert "^demo-canary-[A-Za-z0-9_-]+$" in source
    assert '$script:RunId = if ($RunId)' in source


def test_atomic_heartbeat_write_and_uvicorn_pid_restore(tmp_path: Path) -> None:
    module = _module()
    heartbeat = tmp_path / "heartbeat.json"
    module.write_json(heartbeat, {"phase": "RUNNING", "runner_pid": 123})
    assert json.loads(heartbeat.read_text(encoding="utf-8")) == {
        "phase": "RUNNING",
        "runner_pid": 123,
    }
    events = tmp_path / "controller-events.jsonl"
    events.write_text(
        '{"uvicorn_pid": 11}\nnot-json\n{"uvicorn_pid": 22}\n',
        encoding="utf-8",
    )
    assert module.last_uvicorn_pid(events) == 22


def test_atomic_metadata_write_retries_transient_windows_reader_lock(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    target = tmp_path / "managed-canary.json"
    real_replace = module.os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("fixture reader lock")
        return real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", flaky_replace)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    module.write_json(target, {"phase": "RUNNING"})
    assert attempts == 2
    assert json.loads(target.read_text(encoding="utf-8"))["phase"] == "RUNNING"


def test_transient_controller_event_file_lock_is_retried(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(Path, "exists", lambda _self: True)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda _self, **_kwargs: (_ for _ in ()).throw(
            PermissionError("fixture Windows lock")
        ),
    )
    assert module.last_uvicorn_pid(Path("locked.jsonl")) is None


def test_controller_metadata_contains_required_fields() -> None:
    source = CONTROLLER.read_text(encoding="utf-8")
    for field in (
        "run_id",
        "controller_pid",
        "runner_pid",
        "uvicorn_pid",
        "started_at",
        "hard_deadline",
        "phase",
        "heartbeat_at",
        "result_path",
        "artifact_path",
        "exit_code",
        "terminal_reason",
    ):
        assert f'"{field}"' in source


def test_managed_scripts_parse_in_windows_powershell_51() -> None:
    if POWERSHELL is None:
        pytest.skip("Windows PowerShell 5.1 is unavailable")
    quoted = [str(path).replace("'", "''") for path in (LAUNCHER, CANARY)]
    command = (
        "$all=@();"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{quoted[0]}',"
        "[ref]$null,[ref]$all);"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{quoted[1]}',"
        "[ref]$null,[ref]$all);"
        "if($all.Count){$all|%{$_.Message};exit 1}"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
