"""MCP server: lets any MCP-capable AI agent (Claude Code, Claude Desktop, …)
discover, negotiate and pay for services with Lightning, inside the spending
policy configured by its operator.

Run with ``aibtc-nexus mcp``. Configuration comes from the environment, see
``config.py`` and the README.
"""

from __future__ import annotations

import json
import os

import httpx
from mcp.server.mcpserver import MCPServer

from . import __version__
from .buyer import BuyerAgent
from .client import HttpProviderClient
from .config import backend_from_env, policy_from_env
from .errors import NexusError
from .identity import AgentIdentity
from .ledger import Ledger


def build_server(buyer: BuyerAgent | None = None) -> MCPServer:
    if buyer is None:
        identity_path = os.environ.get(
            "NEXUS_IDENTITY_FILE", os.path.expanduser("~/.aibtc-nexus/buyer.key")
        )
        buyer = BuyerAgent(
            AgentIdentity.load_or_create(identity_path, "mcp-buyer"),
            backend_from_env("mcp-buyer"),
            policy=policy_from_env(),
            ledger=Ledger(
                os.environ.get("NEXUS_DB", os.path.expanduser("~/.aibtc-nexus/buyer.db"))
            ),
        )

    server = MCPServer(
        name="aibtc-nexus",
        version=__version__,
        instructions=(
            "Buy API services from other agents with bitcoin over Lightning. "
            "Always call list_services first, and pass the smallest max_price_sats that fits "
            "the task. Spending limits are enforced server-side and cannot be raised from here."
        ),
    )

    @server.tool()
    async def list_services(provider_url: str) -> str:
        """List the services a provider sells, with list prices in satoshis."""
        async with HttpProviderClient(provider_url) as client:
            info = await client.info()
            services = await client.services()
        return json.dumps({"provider": info, "services": services})

    @server.tool()
    async def buy_service(
        provider_url: str,
        service_id: str,
        max_price_sats: int,
        params_json: str = "{}",
        expected_provider_id: str = "",
    ) -> str:
        """Negotiate a price (never above max_price_sats), pay over Lightning and
        return the service result together with the proof of payment."""
        try:
            params = json.loads(params_json or "{}")
            if not isinstance(params, dict):
                raise ValueError("params_json must be a JSON object")
            async with HttpProviderClient(provider_url) as client:
                r = await buyer.purchase(
                    client,
                    service_id,
                    params,
                    max_price_sats=max_price_sats,
                    expected_provider_id=expected_provider_id or None,
                )
        except (NexusError, ValueError, httpx.HTTPError) as e:
            return json.dumps({"ok": False, "error": type(e).__name__, "detail": str(e)})
        return json.dumps(
            {
                "ok": True,
                "result": r.result,
                "price_sats": r.price_sats,
                "list_price_sats": r.list_price_sats,
                "saved_sats": r.saved_sats,
                "routing_fee_sats": r.routing_fee_sats,
                "payment_hash": r.payment_hash,
                "preimage": r.preimage,
            }
        )

    @server.tool()
    async def wallet_status() -> str:
        """Wallet balance, today's spending against the limits, and totals."""
        report = buyer.report()
        report["balance_sats"] = await buyer.backend.get_balance_sats()
        return json.dumps(report)

    return server


def main() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    main()
