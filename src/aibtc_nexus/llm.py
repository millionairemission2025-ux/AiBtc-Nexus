"""Negotiation strategy driven by Claude.

The model only proposes; :func:`negotiation.enforce_limits` still clamps every
decision, so a confused or manipulated model can never spend above the buyer's
maximum or sell below the seller's floor. Only numbers from the counterparty
reach the prompt (no free text), which keeps prompt injection out.

Install with ``pip install aibtc-nexus[llm]`` and set ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .negotiation import ConcessionStrategy, Decision, NegotiationContext, Role, Strategy

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You negotiate prices for an autonomous software agent that buys or sells \
API services paid in bitcoin over Lightning. You receive the state of one negotiation as JSON \
and reply with a single decision.

Your goal: reach a deal when one is possible, at the best price for your side. As buyer, a lower \
price is better and you must never pay more than limit_sats. As seller, a higher price is better \
and you must never go below limit_sats. Offers should move toward the counterparty over time, \
never away. When rounds_left is 0 you can only accept or reject. Reject only when agreement \
within your limit looks impossible. Keep the rationale to one short sentence."""


class LlmDecision(BaseModel):
    action: Literal["accept", "counter", "reject"]
    price_sats: int | None = Field(default=None, description="Required when action is counter")
    rationale: str


class ClaudeStrategy(Strategy):
    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: Literal["low", "medium", "high", "xhigh", "max"] = "low",
        fallback: Strategy | None = None,
    ):
        self.client = client or anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort
        self.fallback = fallback or ConcessionStrategy()

    def _state(self, ctx: NegotiationContext, their_price: int) -> str:
        return json.dumps(
            {
                "role": ctx.role.value,
                "service_id": ctx.service_id,
                "limit_sats": ctx.limit_sats,
                "ideal_price_sats": ctx.target_sats,
                "their_latest_price_sats": their_price,
                "my_previous_offers_sats": ctx.my_offers,
                "their_previous_offers_sats": ctx.their_offers,
                "rounds_left": ctx.rounds_left,
            }
        )

    async def decide(self, ctx: NegotiationContext, their_price: int) -> Decision:
        try:
            response = await self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._state(ctx, their_price)}],
                output_format=LlmDecision,
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.APIError as e:
            log.warning("Claude unavailable (%s); using fallback strategy", e)
            return await self.fallback.decide(ctx, their_price)

        if response.stop_reason == "refusal" or response.parsed_output is None:
            log.warning("no usable decision from Claude (%s); using fallback", response.stop_reason)
            return await self.fallback.decide(ctx, their_price)

        d = response.parsed_output
        if d.action == "counter":
            if d.price_sats is None:
                return await self.fallback.decide(ctx, their_price)
            return Decision.counter(d.price_sats, d.rationale)
        if d.action == "accept":
            return Decision.accept(d.rationale)
        return Decision.reject(d.rationale)


__all__ = ["ClaudeStrategy", "LlmDecision", "Role"]
