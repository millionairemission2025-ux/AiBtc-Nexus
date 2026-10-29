import random

import pytest

from aibtc_nexus.negotiation import (
    ConcessionStrategy,
    Decision,
    NegotiationContext,
    Role,
    Strategy,
    decide_safely,
    enforce_limits,
)


def ctx(role=Role.BUYER, limit=800, target=500, max_rounds=5, **kw):
    return NegotiationContext(role, "svc", limit, target, max_rounds, **kw)


async def test_original_bug_buyer_never_counters_above_budget():
    # The prototype countered 80% of any offer: 2000 -> 1600 with an 800 budget.
    d = await decide_safely(ConcessionStrategy(), ctx(limit=800), 2000)
    assert d.action == "counter" and d.price_sats <= 800


def test_guard_blocks_accept_above_limit():
    d = enforce_limits(Decision.accept(), ctx(limit=800), 900)
    assert d.action == "counter" and d.price_sats == 800
    d = enforce_limits(Decision.accept(), ctx(limit=800, round=5), 900)
    assert d.action == "reject"


def test_guard_clamps_counter_and_keeps_monotonic():
    c = ctx(limit=800, my_offers=[700])
    assert enforce_limits(Decision.counter(5000), c, 2000).price_sats == 800
    assert enforce_limits(Decision.counter(100), c, 2000).price_sats == 700


def test_guard_turns_overshooting_counter_into_accept():
    assert enforce_limits(Decision.counter(750), ctx(), 700).action == "accept"
    seller = ctx(Role.SELLER, limit=600, target=1000)
    assert enforce_limits(Decision.counter(650), seller, 700).action == "accept"


def test_seller_never_goes_below_floor():
    seller = ctx(Role.SELLER, limit=600, target=1000)
    assert enforce_limits(Decision.counter(10), seller, 5).price_sats == 600
    assert enforce_limits(Decision.accept(), seller, 500).action == "counter"


class ChaosStrategy(Strategy):
    def __init__(self, rng):
        self.rng = rng

    async def decide(self, ctx, their_price):
        choice = self.rng.random()
        if choice < 0.2:
            raise RuntimeError("model crashed")
        if choice < 0.5:
            return Decision.accept()
        if choice < 0.6:
            return Decision.reject()
        return Decision.counter(self.rng.randint(-1000, 100_000))


@pytest.mark.parametrize("seed", range(20))
async def test_guard_holds_against_arbitrary_strategy(seed):
    rng = random.Random(seed)
    strategy = ChaosStrategy(rng)
    for _ in range(200):
        role = rng.choice([Role.BUYER, Role.SELLER])
        limit = rng.randint(1, 5000)
        c = ctx(
            role,
            limit=limit,
            target=rng.randint(1, 5000),
            max_rounds=rng.randint(0, 6),
            round=rng.randint(0, 6),
        )
        their = rng.randint(1, 10_000)
        d = await decide_safely(strategy, c, their)
        if d.action == "accept":
            assert their <= limit if role is Role.BUYER else their >= limit
        if d.action == "counter":
            assert c.rounds_left > 0
            assert d.price_sats > 0
            if role is Role.BUYER:
                assert d.price_sats <= limit and d.price_sats < their
            else:
                assert d.price_sats >= limit and d.price_sats > their


async def test_concession_buyer_reaches_limit_on_last_round():
    s = ConcessionStrategy()
    c = ctx(limit=800, target=500, max_rounds=4)
    prices = []
    for _ in range(4):
        d = await decide_safely(s, c, 5000)
        prices.append(d.price_sats)
        c.round += 1
        c.my_offers.append(d.price_sats)
    assert prices == sorted(prices) and prices[0] == 500 and prices[-1] == 800
    assert (await decide_safely(s, c, 5000)).action == "reject"


def test_invalid_beta():
    with pytest.raises(ValueError):
        ConcessionStrategy(beta=0)
