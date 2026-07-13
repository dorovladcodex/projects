from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "paper_soak.ps1"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_soak_script_safety_configuration_is_paper_only() -> None:
    text = _script_text()
    assert 'TEST_MODE = "false"' in text
    assert 'AUTO_PAPER_EXECUTION = "true"' in text
    assert 'BYBIT_ENABLE_TRADING = "false"' in text
    assert 'NEWS_CLASSIFIER_MODE = "codex_cli"' in text
    assert 'MARKET_DATA_PROVIDER = "BYBIT_REST"' in text
    assert 'NEWS_ENABLE_RSS = "true"' in text


def test_soak_script_does_not_use_test_mode_endpoints_or_synthetic_news() -> None:
    text = _script_text()
    assert 'Invoke-Api "/paper/test' not in text
    assert 'Invoke-Api "/signals/test' not in text
    assert 'Invoke-Api "/news/test' not in text
    assert "Invoke-RestMethod -Method POST" not in text


def test_soak_script_declares_required_artifacts_and_parameters() -> None:
    text = _script_text()
    for parameter in ("Hours", "SampleSeconds", "RestartAtPercent", "OutputDirectory"):
        assert f"${parameter}" in text
    for artifact in (
        "status.jsonl",
        "trades.json",
        "candidates.json",
        "uvicorn.stdout.log",
        "uvicorn.stderr.log",
        "summary.json",
        "report.md",
    ):
        assert artifact in text


def test_soak_accounting_and_report_helpers_pass_on_powershell_51() -> None:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-ValidateHelpersOnly",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "HELPERS: PASS" in completed.stdout
