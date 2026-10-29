import json

import anthropic
import httpx2
import pytest

from aibtc_nexus.llm import ClaudeStrategy
from aibtc_nexus.negotiation import NegotiationContext, Role, decide_safely


def claude_client(reply: dict | None = None, status: int = 200, stop_reason: str = "end_turn"):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta")
        if status != 200:
            return httpx2.Response(
                status, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
            )
        content = [] if reply is None else [{"type": "text", "text": json.dumps(reply)}]
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": content,
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    client = anthropic.AsyncAnthropic(
        api_key="test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return client, seen


def buyer_ctx():
    return NegotiationContext(Role.BUYER, "data", 800, 500, 5)


async def test_claude_counter_is_used_and_request_is_well_formed():
    client, seen = claude_client({"action": "counter", "price_sats": 650, "rationale": "fair"})
    d = await decide_safely(ClaudeStrategy(client=client), buyer_ctx(), 1000)
    assert (d.action, d.price_sats) == ("counter", 650)
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default" and seen["beta"] == "server-side-fallback-2026-07-01"
    assert body["output_config"]["format"]["type"] == "json_schema"
    state = json.loads(body["messages"][0]["content"])
    assert state["limit_sats"] == 800 and state["their_latest_price_sats"] == 1000


async def test_overspending_model_is_clamped():
    client, _ = claude_client({"action": "accept", "price_sats": None, "rationale": "yolo"})
    d = await decide_safely(ClaudeStrategy(client=client), buyer_ctx(), 5000)
    assert d.action == "counter" and d.price_sats == 800
    client, _ = claude_client({"action": "counter", "price_sats": 99_999, "rationale": "yolo"})
    d = await decide_safely(ClaudeStrategy(client=client), buyer_ctx(), 5000)
    assert d.price_sats == 800


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": 500},
        {"reply": None, "stop_reason": "refusal"},
        {"reply": {"action": "counter", "price_sats": None, "rationale": "?"}},
    ],
)
async def test_falls_back_to_deterministic_strategy(kwargs):
    client, _ = claude_client(**kwargs)
    d = await decide_safely(ClaudeStrategy(client=client), buyer_ctx(), 1000)
    assert (d.action, d.price_sats) == ("counter", 500)
