"""Hold-invoice escrow, restart safety and payment reconciliation."""

import asyncio

import pytest

from aibtc_nexus import l402
from aibtc_nexus.buyer import BuyerAgent
from aibtc_nexus.client import LocalProviderClient
from aibtc_nexus.errors import BackendError, DuplicatePayment, PaymentError, PaymentPending
from aibtc_nexus.handlers import echo_handler
from aibtc_nexus.identity import AgentIdentity
from aibtc_nexus.ledger import Ledger
from aibtc_nexus.lightning import InvoiceState, PaymentState
from aibtc_nexus.provider import ESCROW, ProviderAgent, Service


def make_provider(network, node, services, **kw):
    kw.setdefault("escrow_poll_interval", 0.005)
    return ProviderAgent(AgentIdentity.generate("p"), node, services, **kw)


async def test_escrow_purchase_settles_after_delivery(network, buyer):
    node = network.create_node("escrow-p")
    seen_balance = {}

    async def handler(params):
        seen_balance["during"] = node.balance_sats  # payment locked, not yet received
        return {"ok": params}

    provider = make_provider(network, node, [Service("s", "", 500, handler, floor_price_sats=300)])
    assert provider.catalog()[0]["escrow"] is True
    r = await buyer.purchase(LocalProviderClient(provider), "s", {"a": 1}, max_price_sats=400)
    assert r.result == {"ok": {"a": 1}}
    assert seen_balance["during"] == 0
    assert node.balance_sats == r.price_sats
    assert provider.escrow_status(r.payment_hash) == {"status": "delivered", "error": None}
    assert provider.ledger.totals()["in_sats"] == r.price_sats
    await provider.wait_idle()


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_escrow_refunds_buyer_when_service_fails(network, buyer, failure):
    async def broken(params):
        if failure == "timeout":
            await asyncio.sleep(10)
        raise RuntimeError("upstream down")

    node = network.create_node("broken")
    provider = make_provider(network, node, [Service("s", "", 200, broken)], handler_timeout=0.05)
    with pytest.raises(PaymentError, match="no funds spent"):
        await buyer.purchase(LocalProviderClient(provider), "s", max_price_sats=200)
    assert network.nodes["buyer"].balance_sats == 10_000  # refunded, fee included
    assert node.balance_sats == 0
    assert buyer.ledger.totals()["out_count"] == 0
    assert provider.ledger.totals()["in_count"] == 0
    jobs = provider.ledger.list_state(ESCROW)
    assert [job["status"] for _, job in jobs] == ["refunded"]
    assert provider.stats()["escrow"] == {"refunded": 1}


async def test_escrow_preimage_without_payment_is_useless(network):
    provider = make_provider(
        network, network.create_node("p"), [Service("s", "", 10, echo_handler)]
    )
    client = LocalProviderClient(provider)
    challenge = await client.invoke("s", {})
    token, _ = l402.parse_challenge(challenge.headers["WWW-Authenticate"])
    payment_hash = l402.read_caveats(token).payment_hash
    leaked = provider.ledger.get_state(ESCROW, payment_hash)["preimage"]
    resp = await client.invoke("s", {}, authorization=l402.authorization_header(token, leaked))
    assert resp.status == 401
    await provider.aclose()


async def test_unpaid_escrow_expires_and_is_canceled(network):
    node = network.create_node("p")
    provider = make_provider(network, node, [Service("s", "", 10, echo_handler)], invoice_expiry=0)
    challenge = await provider.invoke("s", {})
    payment_hash = l402.read_caveats(challenge.body["token"]).payment_hash
    await provider.wait_idle()
    assert provider.escrow_status(payment_hash)["status"] == "expired"
    assert await node.invoice_state(payment_hash) is InvoiceState.CANCELED


async def test_escrow_survives_provider_restart(tmp_path, network, buyer):
    db = str(tmp_path / "provider.db")
    node = network.create_node("restart")
    secret = b"k" * 32
    services = [Service("s", "", 300, echo_handler, floor_price_sats=200)]
    first = make_provider(network, node, services, ledger=Ledger(db), l402_secret=secret)
    first.identity = AgentIdentity.from_seed_hex("11" * 32)
    client = LocalProviderClient(first)

    quote = await buyer.negotiate(client, "s", 250)
    challenge = await client.invoke("s", {"x": 1}, quote_id=quote.quote_id)
    await first.aclose()  # provider goes down before the buyer pays

    pay = asyncio.create_task(
        buyer.pay_and_collect(
            client, quote.provider_id, "s", {"x": 1}, quote.price_sats, quote.quote_id
        )
    )
    await asyncio.sleep(0.05)
    assert not pay.done()  # funds locked in the HTLC, nobody to deliver yet
    assert node.balance_sats == 0

    second = make_provider(network, node, services, ledger=Ledger(db), l402_secret=secret)
    second.identity = first.identity
    client.provider = second  # the buyer now reaches the restarted provider
    await second.start()
    r = await pay
    assert r.result == {"echo": {"x": 1}} and r.price_sats == quote.price_sats
    again = await LocalProviderClient(second).invoke(
        "s", {}, authorization=l402.authorization_header(challenge.body["token"], r.preimage)
    )
    assert again.status == 200 and again.body["result"] == {"echo": {"x": 1}}


async def test_negotiation_and_quotes_survive_restart(tmp_path, network, buyer):
    db = str(tmp_path / "provider.db")
    node = network.create_node("restart2")
    identity = AgentIdentity.generate("p")
    services = [Service("s", "", 1000, echo_handler, floor_price_sats=600, escrow=False)]

    providers = []

    class RestartingClient(LocalProviderClient):
        """Restarts the provider before every negotiation message."""

        async def negotiate(self, envelope):
            self.provider = ProviderAgent(identity, node, services, ledger=Ledger(db))
            providers.append(self.provider)
            return await super().negotiate(envelope)

    client = RestartingClient(ProviderAgent(identity, node, services, ledger=Ledger(db)))
    r = await buyer.purchase(client, "s", max_price_sats=800)
    assert r.rounds > 1 and len(providers) > 2 and r.price_sats <= 800


async def test_reconcile_pending_payments(network):
    buyer_node = network.create_node("b", 1000)
    seller = network.create_node("s")
    buyer = BuyerAgent(AgentIdentity.generate(), buyer_node)
    paid = await seller.create_invoice(100)
    result = await buyer_node.pay_invoice(paid.payment_request, 5)
    buyer.ledger.begin_payment("out", paid.payment_hash, "p", "svc", 100)
    buyer.ledger.begin_payment("out", "ab" * 32, "p", "svc", 50)
    assert await buyer.reconcile_pending() == {paid.payment_hash: "succeeded", "ab" * 32: "failed"}
    assert buyer.ledger.get_payment("out", paid.payment_hash).preimage == result.preimage
    assert buyer.ledger.get_payment("out", "ab" * 32).status == "failed"


class FlakyBackendNode:
    """Wraps a fake node; the payment goes through but the answer is lost."""

    def __init__(self, node):
        self.node = node

    def __getattr__(self, name):
        return getattr(self.node, name)

    async def pay_invoice(self, payment_request, max_fee_sats):
        await self.node.pay_invoice(payment_request, max_fee_sats)
        raise PaymentPending("connection lost")


async def test_unknown_outcome_is_never_paid_twice(network, make_client):
    node = network.create_node("b", 1000)
    buyer = BuyerAgent(AgentIdentity.generate(), FlakyBackendNode(node))
    provider = make_provider(
        network, network.create_node("p"), [Service("s", "", 100, echo_handler, escrow=False)]
    )
    client = make_client(provider)
    quote = await buyer.negotiate(client, "s", 100)
    with pytest.raises(PaymentPending):
        await buyer.pay_and_collect(client, quote.provider_id, "s", {}, 100, quote.quote_id)
    with pytest.raises(DuplicatePayment):
        await buyer.pay_and_collect(client, quote.provider_id, "s", {}, 100, quote.quote_id)
    assert node.balance_sats == 899
    resolved = await buyer.reconcile_pending()
    assert list(resolved.values()) == ["succeeded"]


async def test_fake_hold_invoice_rules(network):
    alice, bob = network.create_node("alice", 100), network.create_node("bob")
    preimage = "33" * 32
    import hashlib

    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    await bob.create_hold_invoice(10, payment_hash)
    with pytest.raises(BackendError):
        await bob.create_hold_invoice(10, payment_hash)
    with pytest.raises(BackendError):
        await bob.settle_hold_invoice(preimage)  # nothing accepted yet
    pay = asyncio.create_task(
        alice.pay_invoice(
            (
                await bob.create_hold_invoice(5, hashlib.sha256(b"x" * 32).hexdigest())
            ).payment_request,
            5,
        )
    )
    await asyncio.sleep(0)
    assert alice.balance_sats == 94  # locked
    assert (await alice.payment_status(hashlib.sha256(b"x" * 32).hexdigest())).state is (
        PaymentState.PENDING
    )
    await bob.cancel_hold_invoice(hashlib.sha256(b"x" * 32).hexdigest())
    with pytest.raises(PaymentError, match="canceled"):
        await pay
    assert alice.balance_sats == 100


class UnreliableNode:
    """Wraps a fake node; settle/cancel/poll fail a few times like a node restart."""

    def __init__(self, node, failures=3):
        self.node = node
        self.failures = {"settle": failures, "cancel": failures, "state": failures}

    def __getattr__(self, name):
        return getattr(self.node, name)

    def _maybe_fail(self, what):
        if self.failures[what] > 0:
            self.failures[what] -= 1
            raise BackendError(f"{what}: node unavailable")

    async def invoice_state(self, payment_hash):
        self._maybe_fail("state")
        return await self.node.invoice_state(payment_hash)

    async def settle_hold_invoice(self, preimage):
        self._maybe_fail("settle")
        return await self.node.settle_hold_invoice(preimage)

    async def cancel_hold_invoice(self, payment_hash):
        self._maybe_fail("cancel")
        return await self.node.cancel_hold_invoice(payment_hash)


async def test_escrow_retries_settle_and_poll_until_node_returns(network, buyer):
    node = UnreliableNode(network.create_node("shaky"))
    provider = make_provider(network, node, [Service("s", "", 100, echo_handler)])
    r = await buyer.purchase(LocalProviderClient(provider), "s", {"k": 1}, max_price_sats=100)
    assert r.result == {"echo": {"k": 1}} and node.failures["settle"] == 0
    assert node.node.balance_sats == 100


async def test_escrow_retries_refund_and_quote_gets_fresh_invoice(network, buyer):
    calls = {"n": 0}

    async def fails_once(params):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("temporary")
        return "ok"

    node = UnreliableNode(network.create_node("shaky2"))
    node.failures["state"] = 0
    provider = make_provider(
        network, node, [Service("s", "", 300, fails_once, floor_price_sats=200)]
    )
    client = LocalProviderClient(provider)
    quote = await buyer.negotiate(client, "s", 250)
    with pytest.raises(PaymentError, match="no funds spent"):
        await buyer.pay_and_collect(
            client, quote.provider_id, "s", {}, quote.price_sats, quote.quote_id
        )
    assert node.failures["cancel"] == 0
    assert network.nodes["buyer"].balance_sats == 10_000
    await provider.wait_idle()
    # Same quote, second try: a new invoice is issued and the purchase succeeds.
    r = await buyer.pay_and_collect(
        client, quote.provider_id, "s", {}, quote.price_sats, quote.quote_id
    )
    assert r.result == "ok"


def test_escrow_requires_capable_backend():
    from aibtc_nexus.errors import NexusError

    class PlainNode:
        supports_hold_invoices = False

    with pytest.raises(NexusError, match="escrow"):
        ProviderAgent(
            AgentIdentity.generate(), PlainNode(), [Service("s", "", 10, echo_handler, escrow=True)]
        )
    provider = ProviderAgent(
        AgentIdentity.generate(), PlainNode(), [Service("s", "", 10, echo_handler)]
    )
    assert provider.catalog()[0]["escrow"] is False
