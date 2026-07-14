from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bybit.demo import BybitDemoRestClient, DemoExecutionService  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.persistence import PersistenceRepository  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Exact durable-execution Bybit Demo emergency cleanup"
    )
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--confirm-cleanup", action="store_true")
    args = parser.parse_args()
    if not args.confirm_cleanup:
        print("DIRECT DEMO CLEANUP: REFUSED (confirmation required)")
        return 1
    try:
        settings = get_settings()
        repository = PersistenceRepository(settings.database_url, create_schema=False)
        client = BybitDemoRestClient(
            api_key=settings.bybit_api_key, api_secret=settings.bybit_api_secret,
            base_url=settings.bybit_private_demo_base_url,
            private_ws_url=settings.bybit_private_demo_ws_url,
            recv_window_ms=settings.bybit_private_recv_window_ms,
            timeout_seconds=settings.bybit_private_timeout_seconds,
        )
        service = DemoExecutionService(
            settings, repository, client, run_id=settings.demo_run_id
        )
        record = service.direct_cleanup_execution(
            args.execution_id, "FastAPI restart failed; direct guarded cleanup"
        )
        deadline = time.monotonic() + 60
        terminal = {
            "DEMO_CLOSED", "DEMO_CLOSED_AFTER_FAILURE",
            "DEMO_CLOSED_AFTER_INTERRUPTION", "DEMO_CLOSED_EXTERNALLY",
            "DEMO_FAILED_FLAT_VERIFIED",
        }
        while time.monotonic() < deadline:
            service.reconcile()
            record = next(
                item for item in repository.load_demo_executions()
                if str(item.id) == args.execution_id
            )
            positions = [
                item for item in client.get_positions(symbol=record.symbol)
                if Decimal(str(item.get("size") or "0")) > 0
            ]
            orders = client.get_open_orders(symbol=record.symbol)
            if not positions and not orders and record.state.value in terminal:
                break
            time.sleep(2)
        else:
            raise RuntimeError("direct cleanup was not authoritatively verified flat")
        print(f"EXECUTION ID: {record.id}")
        print(f"DURABLE STATE: {record.state.value}")
        print("DIRECT DEMO CLEANUP: PASS")
        return 0
    except Exception as exc:
        print(f"DIRECT DEMO CLEANUP: FAIL\nERROR: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
