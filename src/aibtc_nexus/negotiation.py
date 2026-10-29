"""Negotiation strategies and the hard guard that keeps them inside their limits.

A strategy (deterministic or LLM-driven) proposes a :class:`Decision`. Before
anything is sent, :func:`enforce_limits` rewrites decisions that would break the
agent's limits: a buyer never accepts or offers more than its maximum, a seller
never goes below its floor, offers only move toward the counterparty, and no
counter-offer is made once the round budget is used up.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

log = logging.getLogger(__name__)


class Role(str, Enum):
    BUYER = "buyer"
    SELLER = "seller"


@dataclass
class NegotiationContext:
    role: Role
    service_id: str
    limit_sats: int  # buyer: maximum it will pay; seller: floor it will accept
    target_sats: int  # buyer: opening bid; seller: list price
    max_rounds: int  # counter-offers this side may make
    round: int = 0  # counter-offers this side has already made
    my_offers: list[int] = field(default_factory=list)
    their_offers: list[int] = field(default_factory=list)

    @property
    def rounds_left(self) -> int:
        return max(0, self.max_rounds - self.round)


@dataclass(frozen=True)
class Decision:
    action: Literal["accept", "counter", "reject"]
    price_sats: int | None = None
    rationale: str = ""

    @classmethod
    def accept(cls, rationale: str = "") -> Decision:
        return cls("accept", None, rationale)

    @classmethod
    def counter(cls, price_sats: int, rationale: str = "") -> Decision:
        return cls("counter", int(price_sats), rationale)

    @classmethod
    def reject(cls, rationale: str = "") -> Decision:
        return cls("reject", None, rationale)


class Strategy(abc.ABC):
    @abc.abstractmethod
    async def decide(self, ctx: NegotiationContext, their_price: int) -> Decision:
        """Respond to the counterparty's latest price."""


class ConcessionStrategy(Strategy):
    """Time-dependent concession: moves from target toward limit over the rounds.

    ``beta`` < 1 concedes late (tough), ``beta`` > 1 concedes early (eager),
    ``beta`` == 1 concedes linearly.
    """

    def __init__(self, beta: float = 1.0):
        if beta <= 0:
            raise ValueError("beta must be positive")
        self.beta = beta

    def planned_price(self, ctx: NegotiationContext) -> int:
        # The seller's opening (list-price) offer is already in my_offers, so its
        # first counter is step 1; the buyer's opening bid is step 0.
        step = len(ctx.my_offers)
        steps = max(1, ctx.max_rounds if ctx.role is Role.SELLER else ctx.max_rounds - 1)
        t = min(1.0, step / steps)
        frac = t ** (1 / self.beta)
        price = ctx.target_sats + (ctx.limit_sats - ctx.target_sats) * frac
        return max(1, round(price))

    async def decide(self, ctx: NegotiationContext, their_price: int) -> Decision:
        mine = self.planned_price(ctx)
        good_enough = their_price <= mine if ctx.role is Role.BUYER else their_price >= mine
        if good_enough:
            return Decision.accept(f"{their_price} is at least as good as my next offer {mine}")
        if ctx.rounds_left == 0:
            within = (
                their_price <= ctx.limit_sats
                if ctx.role is Role.BUYER
                else their_price >= ctx.limit_sats
            )
            if within:
                return Decision.accept("out of rounds, price within limit")
            return Decision.reject("out of rounds, price outside limit")
        return Decision.counter(mine, f"round {ctx.round + 1}/{ctx.max_rounds}")


def enforce_limits(decision: Decision, ctx: NegotiationContext, their_price: int) -> Decision:
    """Rewrite ``decision`` so it can never break the agent's limits."""
    buyer = ctx.role is Role.BUYER

    def within_limit(price: int) -> bool:
        return price <= ctx.limit_sats if buyer else price >= ctx.limit_sats

    if decision.action == "reject":
        return decision

    if decision.action == "accept":
        if within_limit(their_price):
            return decision
        if ctx.rounds_left > 0 and ctx.limit_sats != their_price:
            decision = Decision.counter(ctx.limit_sats, "guard: accept outside limit replaced")
        else:
            return Decision.reject("guard: price outside limit")

    # counter-offer
    if ctx.rounds_left == 0:
        if within_limit(their_price):
            return Decision.accept("guard: no rounds left, price within limit")
        return Decision.reject("guard: no rounds left")
    price = decision.price_sats or 0
    if buyer:
        price = min(price, ctx.limit_sats)
        if ctx.my_offers:
            price = max(price, ctx.my_offers[-1])  # never move away from the seller
        if price >= their_price:
            return Decision.accept("guard: counter would meet or exceed their price")
    else:
        price = max(price, ctx.limit_sats)
        if ctx.my_offers:
            price = min(price, ctx.my_offers[-1])
        if price <= their_price:
            return Decision.accept("guard: counter would meet or undercut their price")
    if price <= 0:
        return Decision.reject("guard: non-positive price")
    if price != decision.price_sats:
        log.debug("guard adjusted counter %s -> %s", decision.price_sats, price)
    return Decision.counter(price, decision.rationale)


async def decide_safely(
    strategy: Strategy,
    ctx: NegotiationContext,
    their_price: int,
    fallback: Strategy | None = None,
) -> Decision:
    """Ask ``strategy``; fall back on error; always apply :func:`enforce_limits`."""
    try:
        decision = await strategy.decide(ctx, their_price)
    except Exception:
        log.exception("strategy %s failed; using fallback", type(strategy).__name__)
        decision = await (fallback or ConcessionStrategy()).decide(ctx, their_price)
    return enforce_limits(decision, ctx, their_price)
