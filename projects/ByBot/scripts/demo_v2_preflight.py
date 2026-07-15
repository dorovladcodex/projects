from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import (
    DemoDiagnosticsConfig, ReadOnlyBybitDemoClient, RESOLVED_STATES,
)
from app.config import Settings
from app.db.persistence import PersistenceRepository
from app.models import Symbol
from app.v2.universe import BybitPublicUniverseClient, SymbolUniverseService


def main() -> int:
    try:
        config = DemoDiagnosticsConfig.load(env_path=Path(".env"))
        repository = PersistenceRepository(config.database_url, create_schema=False)
        if not repository.available:
            raise RuntimeError("PostgreSQL persistence is unavailable")
        client = ReadOnlyBybitDemoClient(
            config.api_key, config.api_secret, base_url=config.rest_url
        )
        try:
            client.verify()
        except Exception as exc:
            raise RuntimeError("Demo account verification failed") from exc
        durable_executions = repository.load_demo_executions()
        kill_switch = repository.load_demo_kill_switch() or {"active": False, "reasons": []}
        try:
            remote_orders = client.get_open_orders()
        except Exception as exc:
            raise RuntimeError("Demo open-order reconciliation failed") from exc
        known_order_ids = {
            value for item in durable_executions
            for value in (item.order_id, item.close_order_id) if value
        }
        known_links = {
            value for item in durable_executions
            for value in (item.order_link_id, item.close_order_link_id) if value
        }
        bot_orders = [item for item in remote_orders if (
            str(item.get("orderId") or "") in known_order_ids
            or str(item.get("orderLinkId") or "") in known_links
        )]
        unrelated_orders = [item for item in remote_orders if item not in bot_orders]
        # Force a read-only Settings shape even if the operator's .env has the
        # mutation gate disabled. No FastAPI/DemoExecutionService is imported.
        settings = Settings(
            app_env="local", bot_mode="PAPER", execution_mode="PAPER",
            bybit_demo_trading_enabled=False, v2_enabled=True,
            v2_auto_demo_execution=False,
            allowed_symbols=tuple(symbol.value for symbol in Symbol),
        )
        universe = SymbolUniverseService(
            settings,
            BybitPublicUniverseClient(
                settings.v2_public_rest_url,
                timeout_seconds=settings.market_data_timeout_seconds,
            ),
            repository,
        )
        universe.refresh()
        accepted = [symbol.value for symbol in universe.accepted_symbols]
        rejected = [
            {"symbol": symbol.value, "reasons": status.reasons}
            for symbol, status in universe.statuses.items() if not status.accepted
        ]
        try:
            open_positions = [
                item for item in client.get_usdt_positions()
                if float(item.get("size") or 0) > 0
            ]
        except Exception as exc:
            raise RuntimeError("Demo USDT position reconciliation failed") from exc
        private_scope_rejections = []
        for symbol in universe.accepted_symbols:
            try:
                client.get_positions(symbol)
            except Exception as exc:
                private_scope_rejections.append({
                    "symbol": symbol.value,
                    "reasons": [f"Demo position scope unavailable: {type(exc).__name__}"],
                })
        if private_scope_rejections:
            rejected.extend(private_scope_rejections)
            rejected_symbols = {item["symbol"] for item in private_scope_rejections}
            accepted = [symbol for symbol in accepted if symbol not in rejected_symbols]
        unresolved = [str(item.id) for item in durable_executions if item.state not in RESOLVED_STATES]
        blockers = []
        if unresolved:
            blockers.append("unresolved durable Demo executions exist")
        if bool(kill_switch.get("active")):
            blockers.append("Demo kill switch is active")
        if open_positions:
            blockers.append("unattributed remote position exists")
        if bot_orders:
            blockers.append("bot-owned open Demo orders exist")
        if unrelated_orders:
            blockers.append("unrelated open Demo orders exist")
        payload = {
            "ok": not blockers,
            "execution_environment": "BYBIT_DEMO",
            "authenticated_demo_account": True,
            "live_execution_blocked": True,
            "mainnet_execution_blocked": True,
            "testnet_execution_blocked": True,
            "kill_switch_active": bool(kill_switch.get("active")),
            "bot_owned_open_orders": len(bot_orders),
            "unrelated_open_orders": len(unrelated_orders),
            "unresolved_executions": unresolved,
            "accepted_symbols": accepted,
            "rejected_symbols": rejected,
            "blockers": blockers,
            "exchange_mutations_performed": False,
        }
        print(json.dumps(payload, indent=2))
        return 0 if not blockers else 1
    except Exception as exc:
        print(json.dumps({
            "ok": False, "error": type(exc).__name__,
            "message": str(exc)[:250], "exchange_mutations_performed": False,
        }, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
