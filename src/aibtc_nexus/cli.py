"""Command line interface: ``aibtc-nexus <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from . import __version__


def _demo(args: argparse.Namespace) -> int:
    from .buyer import BuyerAgent
    from .client import LocalProviderClient
    from .errors import NegotiationFailed
    from .fees import FeeSchedule
    from .handlers import echo_handler
    from .identity import AgentIdentity
    from .lightning import FakeLightningNetwork
    from .provider import ProviderAgent, Service

    async def run() -> int:
        net = FakeLightningNetwork()
        provider_node = net.create_node("provider")
        buyer_node = net.create_node("buyer", 10_000)
        strategy = None
        if args.llm:
            from .llm import ClaudeStrategy

            strategy = ClaudeStrategy()
        provider = ProviderAgent(
            AgentIdentity.generate("Data_Provider_B"),
            provider_node,
            [
                Service(
                    "market-data",
                    "BTC order book snapshot",
                    args.list_price,
                    echo_handler,
                    floor_price_sats=args.floor,
                )
            ],
            fee_schedule=FeeSchedule(basis_points=100),
        )
        buyer = BuyerAgent(AgentIdentity.generate("Agent_A"), buyer_node, strategy=strategy)
        client = LocalProviderClient(provider)
        print(
            f"Agent_A budget: {args.max_price} sats | list price: {args.list_price} sats "
            f"| provider floor (private): {args.floor} sats\n"
        )
        try:
            quote = await buyer.negotiate(client, "market-data", args.max_price)
        except NegotiationFailed as e:
            print(f"No deal: {e}")
            return 1
        for msg in quote.transcript:
            who = "Agent_A " if msg["sender"] == buyer.identity.agent_id else "Provider"
            price = msg["body"].get("price_sats", "")
            print(f"  [{msg['round']}] {who} {msg['type']:<7} {price}")
        result = await buyer.pay_and_collect(
            client,
            quote.provider_id,
            "market-data",
            {"pair": "BTC/USD"},
            quote.price_sats,
            quote.quote_id,
            quote.list_price_sats,
            quote.rounds,
        )
        print(
            f"\nPaid {result.price_sats} sats (+{result.routing_fee_sats} routing), "
            f"saved {result.saved_sats} sats vs list price."
        )
        print(f"Proof of payment (preimage): {result.preimage}")
        print(f"Result: {json.dumps(result.result)}")
        print(
            f"Buyer balance: {buyer_node.balance_sats} | provider balance: "
            f"{provider_node.balance_sats} | platform fee accrued: "
            f"{provider.ledger.fee_totals()['accrued']} sats"
        )
        return 0

    return asyncio.run(run())


def _keygen(args: argparse.Namespace) -> int:
    from .identity import AgentIdentity

    if os.path.exists(args.out):
        print(f"{args.out} already exists", file=sys.stderr)
        return 1
    identity = AgentIdentity.load_or_create(args.out)
    print(identity.agent_id)
    return 0


def _load_or_create_secret(path: str) -> str:
    """The L402 signing secret must survive restarts, or paid tokens stop working."""
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    secret = os.urandom(32).hex()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    return secret


def _serve_provider(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import backend_from_env
    from .fees import FeeSchedule, FeeSettler, http_invoice_fetcher
    from .handlers import handler_from_spec
    from .identity import AgentIdentity
    from .ledger import Ledger
    from .provider import ProviderAgent, Service
    from .server import create_provider_app

    with open(args.config) as f:
        config = json.load(f)
    services = [
        Service(
            s["service_id"],
            s.get("description", ""),
            int(s["list_price_sats"]),
            handler_from_spec(s.get("handler", "echo")),
            floor_price_sats=s.get("floor_price_sats"),
            max_rounds=int(s.get("max_rounds", 5)),
        )
        for s in config["services"]
    ]
    identity = AgentIdentity.load_or_create(args.identity, config.get("name", "provider"))
    backend = backend_from_env("provider")
    ledger = Ledger(args.db)
    secret = os.environ.get("NEXUS_L402_SECRET") or _load_or_create_secret(args.l402_secret_file)
    if (os.environ.get("NEXUS_LN_BACKEND") or "fake") == "fake":
        print(
            "WARNING: NEXUS_LN_BACKEND is 'fake' - invoices are simulated and no bitcoin moves.",
            file=sys.stderr,
        )
    fee_bps = config.get("platform_fee_bps")
    provider = ProviderAgent(
        identity,
        backend,
        services,
        ledger,
        l402_secret=bytes.fromhex(secret),
        fee_schedule=FeeSchedule(basis_points=int(fee_bps)) if fee_bps else None,
    )
    settler = None
    platform_url = os.environ.get("NEXUS_PLATFORM_URL") or config.get("platform_url")
    if platform_url and fee_bps:
        settler = FeeSettler(ledger, backend, http_invoice_fetcher(platform_url), identity.agent_id)
    app = create_provider_app(provider, settler, admin_token=os.environ.get("NEXUS_ADMIN_TOKEN"))
    print(f"provider agent id: {identity.agent_id}", file=sys.stderr)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _serve_platform(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import backend_from_env
    from .server import create_platform_app

    uvicorn.run(create_platform_app(backend_from_env("platform")), host=args.host, port=args.port)
    return 0


def _buy(args: argparse.Namespace) -> int:
    import httpx

    from .buyer import BuyerAgent
    from .client import HttpProviderClient
    from .config import backend_from_env, policy_from_env
    from .errors import NexusError
    from .identity import AgentIdentity
    from .ledger import Ledger

    async def run() -> int:
        buyer = BuyerAgent(
            AgentIdentity.load_or_create(args.identity, "buyer"),
            backend_from_env("buyer"),
            policy=policy_from_env(),
            ledger=Ledger(args.db),
        )
        try:
            async with HttpProviderClient(args.url) as client:
                r = await buyer.purchase(
                    client, args.service, json.loads(args.params), max_price_sats=args.max_price
                )
        except (NexusError, httpx.HTTPError, ValueError) as e:
            print(f"{type(e).__name__}: {e}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "result": r.result,
                    "price_sats": r.price_sats,
                    "routing_fee_sats": r.routing_fee_sats,
                    "preimage": r.preimage,
                },
                indent=2,
            )
        )
        return 0

    return asyncio.run(run())


def _mcp(args: argparse.Namespace) -> int:
    from .mcp_server import main

    main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aibtc-nexus", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="two agents negotiate and pay on a simulated network")
    p.add_argument("--max-price", type=int, default=800)
    p.add_argument("--list-price", type=int, default=1000)
    p.add_argument("--floor", type=int, default=650)
    p.add_argument("--llm", action="store_true", help="buyer negotiates with Claude")
    p.set_defaults(func=_demo)

    p = sub.add_parser("keygen", help="create an agent identity key")
    p.add_argument("--out", required=True)
    p.set_defaults(func=_keygen)

    p = sub.add_parser("serve-provider", help="sell services over HTTP + Lightning")
    p.add_argument("--config", required=True, help="services JSON file")
    p.add_argument("--identity", default="provider.key")
    p.add_argument("--db", default="provider.db")
    p.add_argument("--l402-secret-file", default="l402.secret")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8402)
    p.set_defaults(func=_serve_provider)

    p = sub.add_parser("serve-platform", help="collect platform fees from providers")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8403)
    p.set_defaults(func=_serve_platform)

    p = sub.add_parser("buy", help="negotiate, pay and call a provider's service")
    p.add_argument("url")
    p.add_argument("service")
    p.add_argument("--max-price", type=int, required=True)
    p.add_argument("--params", default="{}")
    p.add_argument("--identity", default="buyer.key")
    p.add_argument("--db", default="buyer.db")
    p.set_defaults(func=_buy)

    p = sub.add_parser("mcp", help="run the MCP server on stdio")
    p.set_defaults(func=_mcp)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
