from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bybit.demo_diagnostics import (  # noqa: E402
    DemoDiagnosticsConfig,
    DemoDiagnosticsError,
    format_demo_diagnostics,
    run_demo_diagnostics,
)


def main() -> int:
    try:
        result = run_demo_diagnostics(DemoDiagnosticsConfig.load())
        print(format_demo_diagnostics(result))
        return 0 if result.passed else 1
    except Exception as exc:
        error = exc if isinstance(exc, DemoDiagnosticsError) else type(exc).__name__
        print(f"READ-ONLY DIAGNOSTICS: FAIL\nERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
