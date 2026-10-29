import httpx
import pytest

from aibtc_nexus.buyer import BuyerAgent
from aibtc_nexus.client import HttpProviderClient
from aibtc_nexus.fees import FeeSchedule
from aibtc_nexus.handlers import echo_handler
from aibtc_nexus.identity import AgentIdentity
from aibtc_nexus.lightning import FakeLightningNetwork
from aibtc_nexus.provider import ProviderAgent, Service
from aibtc_nexus.server import create_provider_app


@pytest.fixture
def network():
    return FakeLightningNetwork(routing_fee_sats=1)


@pytest.fixture
def services():
    return [
        Service("data", "negotiable data feed", 1000, echo_handler, floor_price_sats=600),
        Service("fixed", "fixed-price lookup", 300, echo_handler),
    ]


@pytest.fixture
def provider(network, services):
    return ProviderAgent(
        AgentIdentity.generate("provider"),
        network.create_node("provider"),
        services,
        fee_schedule=FeeSchedule(basis_points=100),
    )


@pytest.fixture
def buyer(network):
    return BuyerAgent(AgentIdentity.generate("buyer"), network.create_node("buyer", 10_000))


def http_client_for(provider, **app_kwargs):
    app_kwargs.setdefault("rate_limit_per_second", None)
    app = create_provider_app(provider, **app_kwargs)
    return HttpProviderClient("http://provider.test", transport=httpx.ASGITransport(app=app))


@pytest.fixture
def make_client():
    return http_client_for


@pytest.fixture
def client(provider):
    return http_client_for(provider)
