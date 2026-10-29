"""Signed negotiation messages exchanged between agents.

Every message is an :class:`Envelope` signed with the sender's Ed25519 key over
its canonical JSON form. Receivers check the signature, the clock skew and that
the nonce was not seen before (replay protection).
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import ProtocolError
from .identity import AgentIdentity, verify_signature

PROTOCOL_VERSION = 1
MAX_CLOCK_SKEW_SECONDS = 120


class MessageType:
    RFQ = "rfq"  # buyer asks for a price on a service
    OFFER = "offer"  # provider proposes a price
    COUNTER = "counter"  # buyer proposes a price
    ACCEPT = "accept"  # either side accepts; provider's accept carries the quote
    REJECT = "reject"  # either side walks away

    ALL = frozenset({RFQ, OFFER, COUNTER, ACCEPT, REJECT})


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass
class Envelope:
    type: str
    negotiation_id: str
    round: int
    sender: str
    body: dict[str, Any]
    timestamp: int = field(default_factory=lambda: int(time.time()))
    nonce: str = field(default_factory=lambda: os.urandom(16).hex())
    version: int = PROTOCOL_VERSION
    signature: str = ""

    def signing_payload(self) -> bytes:
        data = asdict(self)
        data.pop("signature")
        return canonical_json(data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Envelope:
        if not isinstance(data, dict):
            raise ProtocolError("envelope must be a JSON object")
        try:
            env = cls(
                type=str(data["type"]),
                negotiation_id=str(data["negotiation_id"]),
                round=int(data["round"]),
                sender=str(data["sender"]),
                body=dict(data["body"]),
                timestamp=int(data["timestamp"]),
                nonce=str(data["nonce"]),
                version=int(data["version"]),
                signature=str(data["signature"]),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ProtocolError(f"malformed envelope: {e}") from e
        if env.type not in MessageType.ALL:
            raise ProtocolError(f"unknown message type {env.type!r}")
        if env.version != PROTOCOL_VERSION:
            raise ProtocolError(f"unsupported protocol version {env.version}")
        return env


def sign(
    identity: AgentIdentity, type: str, negotiation_id: str, round: int, body: dict
) -> Envelope:
    env = Envelope(
        type=type, negotiation_id=negotiation_id, round=round, sender=identity.agent_id, body=body
    )
    env.signature = identity.sign(env.signing_payload())
    return env


class EnvelopeVerifier:
    """Checks signatures, clock skew and nonce reuse."""

    def __init__(self, max_skew: int = MAX_CLOCK_SKEW_SECONDS, nonce_cache_size: int = 100_000):
        self.max_skew = max_skew
        self._nonces: OrderedDict[str, None] = OrderedDict()
        self._cache_size = nonce_cache_size

    def verify(self, env: Envelope, expected_sender: str | None = None) -> Envelope:
        if expected_sender is not None and env.sender != expected_sender:
            raise ProtocolError("message is not from the expected counterparty")
        if not verify_signature(env.sender, env.signing_payload(), env.signature):
            raise ProtocolError("invalid signature")
        if abs(time.time() - env.timestamp) > self.max_skew:
            raise ProtocolError("message timestamp outside allowed clock skew")
        key = f"{env.sender}:{env.nonce}"
        if key in self._nonces:
            raise ProtocolError("replayed message")
        self._nonces[key] = None
        if len(self._nonces) > self._cache_size:
            self._nonces.popitem(last=False)
        return env


def price_from(env: Envelope) -> int:
    try:
        price = int(env.body["price_sats"])
    except (KeyError, TypeError, ValueError) as e:
        raise ProtocolError(f"{env.type} message without a valid price_sats") from e
    if price <= 0:
        raise ProtocolError("price_sats must be positive")
    return price
