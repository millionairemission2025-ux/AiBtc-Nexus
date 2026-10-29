"""Integration test against two real LND nodes (regtest, e.g. with Polar).

Skipped unless these variables are set; the buyer node needs a funded channel
toward the provider node:

    NEXUS_IT_BUYER_LND_URL, NEXUS_IT_BUYER_MACAROON_HEX, NEXUS_IT_BUYER_TLS_CERT
    NEXUS_IT_PROVIDER_LND_URL, NEXUS_IT_PROVIDER_MACAROON_HEX, NEXUS_IT_PROVIDER_TLS_CERT

Run: pytest tests/test_regtest_lnd.py -v
"""

import os

import httpx
import pytest

from aibtc_nexus.buyer import BuyerAgent
from aibtc_nexus.client import HttpProviderClient
from aibtc_nexus.handlers import echo_handler
from aibtc_nexus.identity import AgentIdentity
from aibtc_nexus.lightning import LndRestBackend, preimage_matches
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


async def test_purchase_with_real_lightning_payment():
    buyer_ln, provider_ln = lnd("BUYER"), lnd("PROVIDER")
    provider = ProviderAgent(
        AgentIdentity.generate("provider"),
        provider_ln,
        [Service("data", "regtest data", 1000, echo_handler, floor_price_sats=600)],
    )
    buyer = BuyerAgent(AgentIdentity.generate("buyer"), buyer_ln)
    client = HttpProviderClient(
        "http://provider", transport=httpx.ASGITransport(app=create_provider_app(provider))
    )
    before = await provider_ln.get_balance_sats()
    result = await buyer.purchase(client, "data", {"x": 1}, max_price_sats=800)
    assert 600 <= result.price_sats <= 800
    assert preimage_matches(result.preimage, result.payment_hash)
    assert await provider_ln.is_invoice_settled(result.payment_hash)
    assert await provider_ln.get_balance_sats() == before + result.price_sats
    await buyer_ln.aclose()
    await provider_ln.aclose()
