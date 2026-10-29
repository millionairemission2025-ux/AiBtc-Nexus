import time

import pytest

from aibtc_nexus.errors import ProtocolError
from aibtc_nexus.identity import AgentIdentity, verify_signature
from aibtc_nexus.protocol import Envelope, EnvelopeVerifier, MessageType, price_from, sign


def test_sign_and_verify_roundtrip():
    alice = AgentIdentity.generate()
    env = sign(alice, MessageType.OFFER, "n" * 32, 1, {"price_sats": 100})
    parsed = Envelope.from_dict(env.to_dict())
    assert EnvelopeVerifier().verify(parsed, expected_sender=alice.agent_id) is parsed
    assert price_from(parsed) == 100


def test_tampered_body_is_rejected():
    alice = AgentIdentity.generate()
    data = sign(alice, MessageType.OFFER, "n" * 32, 1, {"price_sats": 100}).to_dict()
    data["body"]["price_sats"] = 1
    with pytest.raises(ProtocolError, match="signature"):
        EnvelopeVerifier().verify(Envelope.from_dict(data))


def test_impersonation_is_rejected():
    alice, mallory = AgentIdentity.generate(), AgentIdentity.generate()
    env = sign(mallory, MessageType.OFFER, "n" * 32, 1, {"price_sats": 100})
    with pytest.raises(ProtocolError, match="expected counterparty"):
        EnvelopeVerifier().verify(env, expected_sender=alice.agent_id)
    env.sender = alice.agent_id  # claim to be alice without her key
    with pytest.raises(ProtocolError, match="signature"):
        EnvelopeVerifier().verify(env)


def test_replay_is_rejected():
    alice = AgentIdentity.generate()
    env = sign(alice, MessageType.RFQ, "n" * 32, 0, {"service_id": "x"})
    verifier = EnvelopeVerifier()
    verifier.verify(Envelope.from_dict(env.to_dict()))
    with pytest.raises(ProtocolError, match="replayed"):
        verifier.verify(Envelope.from_dict(env.to_dict()))


def test_stale_message_is_rejected():
    alice = AgentIdentity.generate()
    env = Envelope(
        MessageType.RFQ, "n" * 32, 0, alice.agent_id, {}, timestamp=int(time.time()) - 3600
    )
    env.signature = alice.sign(env.signing_payload())
    with pytest.raises(ProtocolError, match="skew"):
        EnvelopeVerifier().verify(env)


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"type": "offer"},
        {**sign(AgentIdentity.generate(), "offer", "n", 1, {}).to_dict(), "type": "hack"},
    ],
)
def test_malformed_envelopes(data):
    with pytest.raises(ProtocolError):
        Envelope.from_dict(data)


@pytest.mark.parametrize("body", [{}, {"price_sats": "abc"}, {"price_sats": 0}, {"price_sats": -5}])
def test_invalid_prices(body):
    env = sign(AgentIdentity.generate(), MessageType.COUNTER, "n", 2, body)
    with pytest.raises(ProtocolError):
        price_from(env)


def test_identity_persistence(tmp_path):
    path = tmp_path / "keys" / "agent.key"
    a = AgentIdentity.load_or_create(path)
    b = AgentIdentity.load_or_create(path)
    assert a.agent_id == b.agent_id
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert verify_signature(a.agent_id, b"x", b.sign(b"x"))
    assert not verify_signature("zz", b"x", b.sign(b"x"))
