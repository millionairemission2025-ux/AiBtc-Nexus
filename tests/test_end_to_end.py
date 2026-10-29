import httpx
import pytest

from aibtc_nexus import l402
from aibtc_nexus.buyer import BuyerAgent
from aibtc_nexus.client import HttpProviderClient, LocalProviderClient
from aibtc_nexus.errors import (
    DeliveryFailed,
    InvoiceMismatch,
    NegotiationFailed,
    PolicyViolation,
    ProtocolError,
    ServiceError,
)
from aibtc_nexus.fees import FeeSettler
from aibtc_nexus.handlers import echo_handler
from aibtc_nexus.identity import AgentIdentity
from aibtc_nexus.ledger import Ledger
from aibtc_nexus.policy import SpendingPolicy
from aibtc_nexus.protocol import MessageType, sign
from aibtc_nexus.provider import ProviderAgent, Service
from aibtc_nexus.server import create_platform_app, create_provider_app


async def test_negotiated_purchase_over_http(provider, buyer, client, network):
    r = await buyer.purchase(client, "data", {"q": "btc"}, max_price_sats=800)
    assert 600 <= r.price_sats <= 800
    assert r.list_price_sats == 1000 and r.saved_sats == 1000 - r.price_sats
    assert r.result == {"echo": {"q": "btc"}}
    assert network.nodes["buyer"].balance_sats == 10_000 - r.price_sats - r.routing_fee_sats
    assert network.nodes["provider"].balance_sats == r.price_sats
    assert provider.ledger.totals()["in_sats"] == r.price_sats
    assert provider.ledger.fee_totals()["accrued"] == -(-r.price_sats // 100)
    assert buyer.report()["negotiations"] == {"agreed": 1}


async def test_fixed_price_service(buyer, client):
    r = await buyer.purchase(client, "fixed", max_price_sats=500)
    assert r.price_sats == 300 and r.rounds == 0
    with pytest.raises(NegotiationFailed):
        await buyer.purchase(client, "fixed", max_price_sats=299)


async def test_budget_below_floor_pays_nothing(provider, buyer, client, network):
    with pytest.raises(NegotiationFailed):
        await buyer.purchase(client, "data", max_price_sats=400)
    assert network.nodes["buyer"].balance_sats == 10_000
    assert provider.ledger.negotiation_stats() == {"rejected": 1}


async def test_local_transport(provider, buyer):
    r = await buyer.purchase(LocalProviderClient(provider), "data", max_price_sats=1000)
    assert r.price_sats <= 1000


async def test_unknown_service(buyer, client):
    with pytest.raises(ProtocolError, match="unknown service"):
        await buyer.purchase(client, "nope", max_price_sats=100)


async def test_pinned_provider_identity(buyer, client):
    with pytest.raises(ProtocolError, match="pinned"):
        await buyer.purchase(client, "data", max_price_sats=800, expected_provider_id="00" * 32)


async def test_policy_blocks_payment(network, client):
    buyer = BuyerAgent(
        AgentIdentity.generate(),
        network.create_node("frugal", 10_000),
        policy=SpendingPolicy(daily_limit_sats=500),
    )
    with pytest.raises(PolicyViolation, match="daily"):
        await buyer.purchase(client, "data", max_price_sats=800)
    assert network.nodes["frugal"].balance_sats == 10_000
    buyer.policy.freeze()
    with pytest.raises(PolicyViolation, match="frozen"):
        await buyer.purchase(client, "fixed", max_price_sats=800)


class OverchargingProvider(ProviderAgent):
    async def _challenge(self, service, price, quote, params):
        return await super()._challenge(service, price + 500, quote, params)


async def test_buyer_refuses_inflated_invoice(network, services, buyer, make_client):
    evil = OverchargingProvider(AgentIdentity.generate(), network.create_node("evil"), services)
    with pytest.raises(InvoiceMismatch):
        await buyer.purchase(make_client(evil), "data", max_price_sats=800)
    assert network.nodes["buyer"].balance_sats == 10_000


async def test_delivery_is_idempotent_and_forged_preimage_rejected(
    provider, buyer, client, network
):
    quote = await buyer.negotiate(client, "data", 800)
    challenge = await client.invoke("data", {}, quote_id=quote.quote_id)
    again = await client.invoke("data", {}, quote_id=quote.quote_id)
    assert challenge.status == 402 and challenge.body["token"] == again.body["token"]
    token, pr = l402.parse_challenge(challenge.headers["www-authenticate"])

    forged = await client.invoke(
        "data", {}, authorization=l402.authorization_header(token, "00" * 32)
    )
    assert forged.status == 401

    paid = await network.nodes["buyer"].pay_invoice(pr, 10)
    auth = l402.authorization_header(token, paid.preimage)
    first = await client.invoke("data", {"n": 1}, authorization=auth)
    second = await client.invoke("data", {"n": 1}, authorization=auth)
    assert first.status == second.status == 200 and first.body == second.body
    assert provider.ledger.totals()["in_count"] == 1  # sold once, delivered twice


async def test_token_for_other_service_rejected(buyer, client, network):
    challenge = await client.invoke("fixed", {})
    token, pr = l402.parse_challenge(challenge.headers["www-authenticate"])
    paid = await network.nodes["buyer"].pay_invoice(pr, 10)
    resp = await client.invoke(
        "data", {}, authorization=l402.authorization_header(token, paid.preimage)
    )
    assert resp.status == 401


async def test_unpaid_invoice_needs_settlement(network, buyer, make_client):
    provider = ProviderAgent(
        AgentIdentity.generate(),
        network.create_node("direct"),
        [Service("fixed", "", 300, echo_handler, escrow=False)],
    )
    client = make_client(provider)
    challenge = await client.invoke("fixed", {})
    token, _ = l402.parse_challenge(challenge.headers["www-authenticate"])
    caveats = l402.read_caveats(token)
    assert not caveats.escrow
    # Learn the preimage without paying (as a compromised node log could): still refused.
    preimage = provider.backend._invoices[caveats.payment_hash].preimage
    resp = await client.invoke(
        "fixed", {}, authorization=l402.authorization_header(token, preimage)
    )
    assert resp.status == 401 and "not settled" in resp.body["detail"]


async def test_failed_delivery_keeps_proof_and_can_be_retried(network, buyer, make_client):
    calls = {"n": 0}

    async def flaky(params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("upstream down")
        return "ok"

    provider = ProviderAgent(
        AgentIdentity.generate(),
        network.create_node("flaky"),
        [Service("s", "", 100, flaky, escrow=False)],
    )
    buyer.delivery_retries = 1
    client = make_client(provider)
    with pytest.raises(DeliveryFailed) as err:
        await buyer.purchase(client, "s", max_price_sats=100)
    assert isinstance(err.value, ServiceError)
    assert err.value.preimage in str(err.value)
    r = await buyer.retry_delivery(client, err.value)
    assert r.result == "ok" and r.payment_hash == err.value.payment_hash
    assert buyer.ledger.totals()["out_count"] == 1  # paid exactly once
    assert network.nodes["flaky"].balance_sats == 100


async def test_protocol_abuse_is_rejected(provider, client):
    mallory = AgentIdentity.generate()
    env = sign(mallory, MessageType.COUNTER, "x" * 32, 2, {"price_sats": 1})
    with pytest.raises(ProtocolError, match="unknown or closed"):
        await client.negotiate(env.to_dict())
    rfq = sign(mallory, MessageType.RFQ, "y" * 32, 0, {"service_id": "data"})
    await client.negotiate(rfq.to_dict())
    with pytest.raises(ProtocolError, match="already exists"):
        await client.negotiate(
            sign(mallory, MessageType.RFQ, "y" * 32, 0, {"service_id": "data"}).to_dict()
        )
    with pytest.raises(ProtocolError, match="not offered"):
        await client.negotiate(
            sign(mallory, MessageType.ACCEPT, "y" * 32, 2, {"price_sats": 1}).to_dict()
        )
    other = AgentIdentity.generate()
    with pytest.raises(ProtocolError, match="negotiating buyer"):
        await client.negotiate(
            sign(other, MessageType.COUNTER, "y" * 32, 2, {"price_sats": 900}).to_dict()
        )
    with pytest.raises(ProtocolError, match="expected round"):
        await client.negotiate(
            sign(mallory, MessageType.COUNTER, "y" * 32, 4, {"price_sats": 900}).to_dict()
        )
    bad_quote = await client.invoke("data", {}, quote_id="deadbeef")
    assert bad_quote.status == 400


async def test_http_edge_cases(provider, make_client):
    app_client = make_client(provider, admin_token="secret")
    raw = app_client._client
    assert (await raw.get("/healthz")).json() == {"ok": True}
    assert (await raw.post("/v1/negotiate", content=b"not json")).status_code == 400
    assert (await raw.post("/v1/services/data/invoke", json={"params": []})).status_code == 400
    assert (await raw.post("/v1/services/zzz/invoke", json={})).status_code == 404
    assert (await raw.get("/v1/admin/stats")).status_code == 401
    stats = await raw.get("/v1/admin/stats", headers={"Authorization": "Bearer secret"})
    assert stats.status_code == 200 and "sales" in stats.json()
    services = await app_client.services()
    assert {s["service_id"] for s in services} == {"data", "fixed"}
    assert all("floor_price_sats" not in s for s in services)  # floor stays private


async def test_platform_fee_settlement(network, provider, buyer, client):
    for _ in range(3):
        await buyer.purchase(client, "data", max_price_sats=800)
    accrued = provider.ledger.fee_totals()["accrued"]
    assert accrued > 0

    platform_node = network.create_node("platform")
    platform = create_platform_app(platform_node)

    async def fetch_via_asgi(amount, provider_id):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=platform), base_url="http://platform.test"
        ) as c:
            resp = await c.post(
                "/v1/platform/fee-invoice", json={"amount_sats": amount, "provider_id": provider_id}
            )
            return resp.json()["payment_request"]

    settler = FeeSettler(
        provider.ledger,
        provider.backend,
        fetch_via_asgi,
        provider.identity.agent_id,
        threshold_sats=10_000,
    )
    assert await settler.settle() == 0  # below threshold
    assert await settler.settle(force=True) == accrued
    assert platform_node.balance_sats == accrued
    assert provider.ledger.fee_totals() == {"accrued": 0, "settled": accrued}
    assert await settler.settle(force=True) == 0


async def test_platform_rejects_bad_amounts(network):
    app = create_platform_app(network.create_node("platform"), max_invoice_sats=1000)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://p") as c:
        assert (
            await c.post("/v1/platform/fee-invoice", json={"amount_sats": 0})
        ).status_code == 400
        assert (
            await c.post("/v1/platform/fee-invoice", json={"amount_sats": 5000})
        ).status_code == 400
        assert (await c.post("/v1/platform/fee-invoice", json={})).status_code == 400


async def test_persistent_ledgers(tmp_path, network, services):
    provider = ProviderAgent(
        AgentIdentity.generate(),
        network.create_node("p2"),
        services,
        ledger=Ledger(str(tmp_path / "p.db")),
    )
    buyer = BuyerAgent(
        AgentIdentity.generate(),
        network.create_node("b2", 5000),
        ledger=Ledger(str(tmp_path / "b.db")),
    )
    client = HttpProviderClient(
        "http://x", transport=httpx.ASGITransport(app=create_provider_app(provider))
    )
    await buyer.purchase(client, "fixed", max_price_sats=300)
    assert Ledger(str(tmp_path / "b.db")).totals()["out_sats"] == 300
    assert Ledger(str(tmp_path / "p.db")).totals()["in_sats"] == 300
