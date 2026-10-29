"""Two agents negotiate and settle a purchase on a simulated Lightning network.

pip install -e .
python examples/two_agents.py
"""

import asyncio

from aibtc_nexus.buyer import BuyerAgent
from aibtc_nexus.client import LocalProviderClient
from aibtc_nexus.identity import AgentIdentity
from aibtc_nexus.lightning import FakeLightningNetwork
from aibtc_nexus.policy import SpendingPolicy
from aibtc_nexus.provider import ProviderAgent, Service


async def sentiment(params: dict) -> dict:
    text = params.get("text", "")
    score = sum(w in text.lower() for w in ("up", "moon", "bull")) - text.lower().count("down")
    label = "bullish" if score > 0 else "bearish" if score < 0 else "neutral"
    return {"text": text, "sentiment": label}


async def main() -> None:
    network = FakeLightningNetwork()
    provider = ProviderAgent(
        AgentIdentity.generate("sentiment-provider"),
        network.create_node("provider"),
        [Service("sentiment", "Crypto news sentiment", 500, sentiment, floor_price_sats=300)],
    )
    buyer = BuyerAgent(
        AgentIdentity.generate("trading-agent"),
        network.create_node("buyer", balance_sats=5_000),
        policy=SpendingPolicy(max_payment_sats=1_000, daily_limit_sats=3_000),
    )

    result = await buyer.purchase(
        LocalProviderClient(provider),
        "sentiment",
        {"text": "BTC breaks out, bulls aim for the moon"},
        max_price_sats=400,
    )
    print(f"result: {result.result}")
    print(f"paid {result.price_sats} sats (list {result.list_price_sats}), {result.rounds} rounds")
    print(f"proof of payment: {result.preimage}")
    print(f"buyer report: {buyer.report()}")


if __name__ == "__main__":
    asyncio.run(main())
