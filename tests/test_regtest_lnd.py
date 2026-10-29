"""Integration tests against two real LND nodes (regtest, e.g. with Polar).

Skipped unless these variables are set; the buyer node needs a funded channel
toward the provider node:

    NEXUS_IT_BUYER_LND_URL, NEXUS_IT_BUYER_MACAROON_HEX, NEXUS_IT_BUYER_TLS_CERT
    NEXUS_IT_PROVIDER_LND_URL, NEXUS_IT_PROVIDER_MACAROON_HEX, NEXUS_IT_PROVIDER_TLS_CERT

Run: pytest tests/test_regtest_lnd.py -v
"""

import asyncio
import os

import httpx
import pytest

from aibtc_nexus.buyer import BuyerAgent
from aibtc_nexus.client import HttpProviderClient
from aibtc_nexus.errors import PaymentError
from aibtc_nexus.handlers import echo_handler
from aibtc_nexus.identity import AgentIdentity
from aibtc_nexus.lightning import InvoiceState, LndRestBackend, PaymentState, preimage_matches
from aibtc_nexus.provider import ProviderAgent, Service
from aibtc_nexus.server import create_provider_app

REQUIRED = [
    f"NEXUS_IT_{who}_{what}"
    for who in ("BUYER", "PROVIDER")
    for what in ("LND_URL", "MACAROON_HEX", "TLS_CERT")
]

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(v) for v in REQUIRED), reason="regtest LND nodes not configured"
)


def lnd(who: str) -> LndRestBackend:
    return LndRestBackend(
        os.environ[f"NEXUS_IT_{who}_LND_URL"],
        os.environ[f"NEXUS_IT_{who}_MACAROON_HEX"],
        os.environ[f"NEXUS_IT_{who}_TLS_CERT"],
    )


@pytest.fixture
async def nodes():
    buyer_ln, provider_ln = lnd("BUYER"), lnd("PROVIDER")
    yield buyer_ln, provider_ln
    await buyer_ln.aclose()
    await provider_ln.aclose()


def http_client(provider: ProviderAgent) -> HttpProviderClient:
    app = create_provider_app(provider, rate_limit_per_second=None)
    return HttpProviderClient("http://provider", transport=httpx.ASGITransport(app=app))


async def wait_for_balance(node: LndRestBackend, expected: int, timeout: float = 10) -> int:
    """LND updates channel balances shortly after an invoice settles."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        balance = await node.get_balance_sats()
        if balance == expected or asyncio.get_running_loop().time() > deadline:
            return balance
        await asyncio.sleep(0.2)


async def test_escrow_purchase_with_real_lightning(nodes):
    buyer_ln, provider_ln = nodes
    provider = ProviderAgent(
        AgentIdentity.generate("provider"),
        provider_ln,
        [Service("data", "regtest data", 1000, echo_handler, floor_price_sats=600)],
        escrow_poll_interval=0.2,
    )
    assert provider.catalog()[0]["escrow"] is True  # LND supports hold invoices
    buyer = BuyerAgent(AgentIdentity.generate("buyer"), buyer_ln)
    before = await provider_ln.get_balance_sats()
    result = await buyer.purchase(http_client(provider), "data", {"x": 1}, max_price_sats=800)
    assert 600 <= result.price_sats <= 800
    assert result.result == {"echo": {"x": 1}}
    assert preimage_matches(result.preimage, result.payment_hash)
    assert await provider_ln.invoice_state(result.payment_hash) is InvoiceState.SETTLED
    assert await wait_for_balance(provider_ln, before + result.price_sats) == (
        before + result.price_sats
    )
    status = await buyer_ln.payment_status(result.payment_hash)
    assert status.state is PaymentState.SUCCEEDED and status.preimage == result.preimage
    await provider.aclose()


async def test_escrow_refund_with_real_lightning(nodes):
    buyer_ln, provider_ln = nodes

    async def broken(params):
        raise RuntimeError("upstream down")

    provider = ProviderAgent(
        AgentIdentity.generate("provider"),
        provider_ln,
        [Service("s", "", 500, broken)],
        escrow_poll_interval=0.2,
    )
    buyer = BuyerAgent(AgentIdentity.generate("buyer"), buyer_ln)
    buyer_before = await buyer_ln.get_balance_sats()
    provider_before = await provider_ln.get_balance_sats()
    with pytest.raises(PaymentError, match="no funds spent"):
        await buyer.purchase(http_client(provider), "s", max_price_sats=500)
    assert await wait_for_balance(buyer_ln, buyer_before) == buyer_before
    assert await provider_ln.get_balance_sats() == provider_before
    await provider.wait_idle()
    jobs = [job["status"] for _, job in provider.ledger.list_state("escrow")]
    assert jobs == ["refunded"]


async def test_direct_payment_and_reconcile_with_real_lightning(nodes):
    buyer_ln, provider_ln = nodes
    provider = ProviderAgent(
        AgentIdentity.generate("provider"),
        provider_ln,
        [Service("fixed", "", 250, echo_handler, escrow=False)],
    )
    buyer = BuyerAgent(AgentIdentity.generate("buyer"), buyer_ln)
    result = await buyer.purchase(http_client(provider), "fixed", {"n": 2}, max_price_sats=300)
    assert result.price_sats == 250 and result.result == {"echo": {"n": 2}}
    assert await provider_ln.is_invoice_settled(result.payment_hash)

    # Simulate a crash right after paying: the ledger only knows "pending".
    invoice = await provider_ln.create_invoice(10, "reconcile")
    paid = await buyer_ln.pay_invoice(invoice.payment_request, 5)
    buyer.ledger.begin_payment("out", invoice.payment_hash, "p", "s", 10)
    buyer.ledger.begin_payment("out", "ab" * 32, "p", "s", 10)  # never sent
    resolved = await buyer.reconcile_pending()
    assert resolved == {invoice.payment_hash: "succeeded", "ab" * 32: "failed"}
    assert buyer.ledger.get_payment("out", invoice.payment_hash).preimage == paid.preimage
