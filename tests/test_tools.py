import json

import httpx
import pytest

from aibtc_nexus import cli, config
from aibtc_nexus.errors import NexusError, ServiceError
from aibtc_nexus.handlers import HttpProxyHandler, echo_handler, handler_from_spec
from aibtc_nexus.lightning import FakeLightningNode, LnbitsBackend, LndRestBackend
from aibtc_nexus.mcp_server import build_server


def test_cli_demo(capsys):
    assert cli.main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "Proof of payment" in out and "saved" in out
    assert cli.main(["demo", "--max-price", "100"]) == 1


def test_cli_keygen(tmp_path, capsys):
    key = tmp_path / "a.key"
    assert cli.main(["keygen", "--out", str(key)]) == 0
    assert len(capsys.readouterr().out.strip()) == 64
    assert cli.main(["keygen", "--out", str(key)]) == 1


def test_backend_from_env(monkeypatch):
    monkeypatch.setenv("NEXUS_LN_BACKEND", "fake")
    assert isinstance(config.backend_from_env("n1"), FakeLightningNode)
    assert config.backend_from_env("n1") is config.backend_from_env("n1")
    monkeypatch.setenv("NEXUS_LN_BACKEND", "lnd")
    with pytest.raises(NexusError, match="LND_REST_URL"):
        config.backend_from_env()
    monkeypatch.setenv("LND_REST_URL", "https://lnd")
    monkeypatch.setenv("LND_MACAROON_HEX", "ab")
    assert isinstance(config.backend_from_env(), LndRestBackend)
    monkeypatch.setenv("NEXUS_LN_BACKEND", "lnbits")
    monkeypatch.setenv("LNBITS_URL", "https://lnbits")
    monkeypatch.setenv("LNBITS_INVOICE_KEY", "k")
    assert isinstance(config.backend_from_env(), LnbitsBackend)
    monkeypatch.setenv("NEXUS_LN_BACKEND", "carrier-pigeon")
    with pytest.raises(NexusError):
        config.backend_from_env()


def test_policy_from_env(monkeypatch):
    monkeypatch.setenv("NEXUS_MAX_PAYMENT_SATS", "123")
    monkeypatch.setenv("NEXUS_ALLOWED_PROVIDERS", "aa, bb")
    monkeypatch.setenv("NEXUS_FROZEN", "true")
    p = config.policy_from_env()
    assert p.max_payment_sats == 123 and p.allowed_counterparties == {"aa", "bb"} and p.frozen


async def test_handlers(monkeypatch):
    assert await echo_handler({"a": 1}) == {"echo": {"a": 1}}
    assert handler_from_spec("echo") is echo_handler
    assert isinstance(handler_from_spec({"type": "http", "url": "http://u"}), HttpProxyHandler)
    with pytest.raises(ValueError):
        handler_from_spec("shell")

    def upstream(request):
        if request.url.path == "/ok":
            return httpx.Response(200, json={"got": json.loads(request.content)})
        return httpx.Response(500)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(upstream), **kw),
    )
    assert await HttpProxyHandler("http://up/ok")({"x": 1}) == {"got": {"x": 1}}
    with pytest.raises(ServiceError):
        await HttpProxyHandler("http://up/fail")({})


async def test_mcp_server_tools(provider, buyer, make_client, monkeypatch):
    from aibtc_nexus import mcp_server

    monkeypatch.setattr(mcp_server, "HttpProviderClient", lambda url: make_client(provider))
    server = build_server(buyer)
    names = {t.name for t in await server.list_tools()}
    assert names == {"list_services", "buy_service", "wallet_status", "reconcile_payments"}

    def text(result):
        blocks = result[0] if isinstance(result, tuple) else result
        if hasattr(blocks, "content"):
            blocks = blocks.content
        return json.loads(blocks[0].text)

    listed = text(await server.call_tool("list_services", {"provider_url": "http://p"}))
    assert {s["service_id"] for s in listed["services"]} == {"data", "fixed"}
    bought = text(
        await server.call_tool(
            "buy_service",
            {
                "provider_url": "http://p",
                "service_id": "data",
                "max_price_sats": 800,
                "params_json": '{"q": 1}',
            },
        )
    )
    assert bought["ok"] and bought["result"] == {"echo": {"q": 1}} and bought["price_sats"] <= 800
    refused = text(
        await server.call_tool(
            "buy_service",
            {"provider_url": "http://p", "service_id": "data", "max_price_sats": 10},
        )
    )
    assert refused == {"ok": False, "error": "NegotiationFailed", "detail": refused["detail"]}
    assert text(await server.call_tool("reconcile_payments", {})) == {"ok": True, "resolved": {}}
    status = text(await server.call_tool("wallet_status", {}))
    assert status["balance_sats"] < 10_000 and status["totals"]["out_count"] == 1


def test_l402_secret_persists(tmp_path):
    path = str(tmp_path / "s")
    first = cli._load_or_create_secret(path)
    assert cli._load_or_create_secret(path) == first and len(bytes.fromhex(first)) == 32


def test_cli_buy_reports_unreachable_provider(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NEXUS_LN_BACKEND", "fake")
    args = [
        "buy",
        "http://127.0.0.1:9",
        "data",
        "--max-price",
        "10",
        "--identity",
        str(tmp_path / "b.key"),
        "--db",
        str(tmp_path / "b.db"),
    ]
    assert cli.main(args) == 1
    assert "ConnectError" in capsys.readouterr().err


def test_cli_reconcile(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NEXUS_LN_BACKEND", "fake")
    args = ["reconcile", "--identity", str(tmp_path / "b.key"), "--db", str(tmp_path / "b.db")]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {"resolved": {}, "still_pending": []}
