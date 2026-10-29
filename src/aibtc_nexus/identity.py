"""Agent identities: Ed25519 key pairs used to sign protocol messages."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class AgentIdentity:
    """An agent's signing key. The agent id is the hex-encoded public key."""

    def __init__(self, private_key: Ed25519PrivateKey, name: str = "agent"):
        self._key = private_key
        self.name = name
        raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.agent_id = raw.hex()

    @classmethod
    def generate(cls, name: str = "agent") -> AgentIdentity:
        return cls(Ed25519PrivateKey.generate(), name)

    @classmethod
    def from_seed_hex(cls, seed_hex: str, name: str = "agent") -> AgentIdentity:
        return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex)), name)

    @classmethod
    def load_or_create(cls, path: str | os.PathLike, name: str = "agent") -> AgentIdentity:
        """Load a key from ``path`` or create one there (file mode 0600)."""
        p = Path(path)
        if p.exists():
            return cls.from_seed_hex(p.read_text().strip(), name)
        identity = cls.generate(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(identity.seed_hex())
        return identity

    def seed_hex(self) -> str:
        return self._key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        ).hex()

    def sign(self, data: bytes) -> str:
        return self._key.sign(data).hex()


def verify_signature(agent_id: str, data: bytes, signature_hex: str) -> bool:
    """Return True if ``signature_hex`` is a valid signature of ``data`` by ``agent_id``."""
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(agent_id))
        public_key.verify(bytes.fromhex(signature_hex), data)
        return True
    except (ValueError, InvalidSignature):
        return False
