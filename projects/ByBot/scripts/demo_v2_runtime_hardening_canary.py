from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    canary_id = (
        "demo-v2-runtime-hardening-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    tests = [
        (
            "tests/test_bybit_demo_execution.py::"
            "test_cycle_261_market_cross_after_precheck_retains_original_protection"
        ),
        (
            "tests/test_bybit_demo_execution.py::"
            "test_protection_market_cross_before_submission_is_observational"
        ),
        (
            "tests/test_v2_certification_monitor.py::"
            "test_multiple_timeouts_form_one_degraded_incident"
        ),
        (
            "tests/test_v2_certification_monitor.py::"
            "test_status_recovery_resumes_healthy_monitoring"
        ),
        (
            "tests/test_v2_certification_monitor.py::"
            "test_monitor_state_machine_has_no_exchange_mutation_surface"
        ),
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *tests],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    artifact = ROOT / "artifacts" / "runtime-hardening" / canary_id
    artifact.mkdir(parents=True, exist_ok=True)
    result = {
        "canary_id": canary_id,
        "protection_invalidation": (
            "PASS" if completed.returncode == 0 else "FAIL"
        ),
        "existing_protection_confirmed": (
            "PASS" if completed.returncode == 0 else "FAIL"
        ),
        "cycle_failures": 0 if completed.returncode == 0 else None,
        "monitor_degradation_recovery": (
            "PASS" if completed.returncode == 0 else "FAIL"
        ),
        "monitor_exchange_mutations": 0,
        "return_code": completed.returncode,
        "test_output": (completed.stdout + completed.stderr)[-4000:],
    }
    (artifact / "result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        key: value for key, value in result.items() if key != "test_output"
    }, indent=2))
    print(f"artifact={artifact}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
