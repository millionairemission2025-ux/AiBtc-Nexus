"""Backend-agnostic Lightning interface."""

from __future__ import annotations

import abc
import hashlib
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Invoice:
    payment_request: str
    payment_hash: str  # hex
    amount_sats: int
    created_at: int  # unix seconds
    expiry: int  # seconds
    memo: str = ""
    payee: str = ""

    @property
    def expires_at(self) -> int:
        return self.created_at + self.expiry

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at


@dataclass(frozen=True)
class PaymentResult:
    payment_hash: str
    preimage: str  # hex
    amount_sats: int
    fee_sats: int


def preimage_matches(preimage_hex: str, payment_hash_hex: str) -> bool:
    """A Lightning payment is proven when sha256(preimage) == payment_hash."""
    try:
        return hashlib.sha256(bytes.fromhex(preimage_hex)).hexdigest() == payment_hash_hex.lower()
    except ValueError:
        return False


class LightningBackend(abc.ABC):
    """What the rest of the package needs from a Lightning wallet or node."""

    @abc.abstractmethod
    async def create_invoice(
        self, amount_sats: int, memo: str = "", expiry: int = 600
    ) -> Invoice: ...

    @abc.abstractmethod
    async def decode_invoice(self, payment_request: str) -> Invoice: ...

    @abc.abstractmethod
    async def pay_invoice(self, payment_request: str, max_fee_sats: int) -> PaymentResult:
        """Pay and return the preimage. Must raise PaymentError on failure."""

    @abc.abstractmethod
    async def is_invoice_settled(self, payment_hash: str) -> bool: ...

    @abc.abstractmethod
    async def get_balance_sats(self) -> int: ...

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""
