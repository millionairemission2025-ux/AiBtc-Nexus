import asyncio

import httpx
from starlette.testclient import TestClient

from aibtc_nexus import l402
from aibtc_nexus.handlers import echo_handler
from aibtc_nexus.identity import AgentIdentity
from aibtc_nexus.lightning import FakeLightningNetwork
from aibtc_nexus.provider import ProviderAgent, Service
from aibtc_nexus.server import MAX_BODY_BYTES, RateLimitMiddleware, create_provider_app


def test_rate_limiter_bucket():
    limiter = RateLimitMiddleware(app=None, rate_per_second=1.0, burst=2)
    assert limiter.allow("a", now=0) and limiter.allow("a", now=0)
    assert not limiter.allow("a", now=0)
    assert limiter.allow("b", now=0)  # per client
    assert limiter.allow("a", now=1.0)  # refilled


async def test_rate_limit_and_body_limit_over_http(provider, make_client):
    client = make_client(provider, rate_limit_per_second=0.001, rate_limit_burst=2)
    raw = client._client
    statuses = [(await raw.post("/v1/negotiate", json={})).status_code for _ in range(3)]
    assert statuses == [400, 400, 429]
    assert (await raw.get("/v1/services")).status_code == 200  # GETs are not limited

    client = make_client(provider)
    big = b'{"params": "' + b"x" * MAX_BODY_BYTES + b'"}'
    resp = await client._client.post("/v1/services/data/invoke", content=big)
    assert resp.status_code == 413


async def test_escrow_status_endpoint(provider, make_client):
    client = make_client(provider)
    challenge = await client.invoke("data", {})
    payment_hash = l402.read_caveats(challenge.body["token"]).payment_hash
    resp = await client._client.get(f"/v1/escrow/{payment_hash}")
    assert resp.json() == {"status": "waiting", "error": None}
    assert "preimage" not in resp.text
    assert (await client._client.get("/v1/escrow/abc")).status_code == 404
    await provider.aclose()


def test_lifespan_resumes_escrow_and_shuts_down(tmp_path):
    from aibtc_nexus.ledger import Ledger

    network = FakeLightningNetwork()
    node = network.create_node("p")
    buyer = network.create_node("b", 1000)
    db = str(tmp_path / "p.db")
    services = [Service("s", "", 100, echo_handler)]
    first = ProviderAgent(
        AgentIdentity.generate(), node, services, ledger=Ledger(db), escrow_poll_interval=0.01
    )

    async def make_challenge():
        resp = await first.invoke("s", {"v": 1})
        await first.aclose()
        return resp.body["payment_request"]

    loop = asyncio.new_event_loop()
    pr = loop.run_until_complete(make_challenge())

    second = ProviderAgent(
        first.identity, node, services, ledger=Ledger(db), escrow_poll_interval=0.01
    )
    with TestClient(create_provider_app(second, rate_limit_per_second=None)) as http:
        assert len(second._tasks) == 1  # resumed by the lifespan
        result = http.portal.call(buyer.pay_invoice, pr, 5)
        assert result.amount_sats == 100
        assert http.get(f"/v1/escrow/{result.payment_hash}").json()["status"] == "delivered"
    assert second._tasks == {}
    loop.close()


async def test_invoke_reports_backend_outage(network, make_client):
    class DownNode:
        supports_hold_invoices = False

        async def create_invoice(self, *a, **k):
            from aibtc_nexus.errors import BackendError

            raise BackendError("node offline")

    provider = ProviderAgent(
        AgentIdentity.generate(), DownNode(), [Service("s", "", 10, echo_handler)]
    )
    resp = await make_client(provider).invoke("s", {})
    assert resp.status == 503


async def test_http_client_reaches_real_server_shape(provider):
    app = create_provider_app(provider, rate_limit_per_second=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        info = (await c.get("/v1/info")).json()
    assert info["escrow"] is True and len(info["agent_id"]) == 64
