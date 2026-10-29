"""Backend-agnostic Lightning interface."""

from __future__ import annotations

import abc
import hashlib
import time
from dataclasses import dataclass
from enum import Enum


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


class InvoiceState(str, Enum):
    OPEN = "open"
    ACCEPTED = "accepted"  # hold invoice: HTLC locked, waiting for settle or cancel
    SETTLED = "settled"
    CANCELED = "canceled"


class PaymentState(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING = "pending"
    NOT_FOUND = "not_found"  # the node never attempted it


@dataclass(frozen=True)
class PaymentStatus:
    state: PaymentState
    preimage: str = ""
    fee_sats: int = 0


def preimage_matches(preimage_hex: str, payment_hash_hex: str) -> bool:
    """A Lightning payment is proven when sha256(preimage) == payment_hash."""
    try:
        return hashlib.sha256(bytes.fromhex(preimage_hex)).hexdigest() == payment_hash_hex.lower()
    except ValueError:
        return False


class LightningBackend(abc.ABC):
    """What the rest of the package needs from a Lightning wallet or node."""

    #: True when the backend can create, settle and cancel hold invoices (escrow).
    supports_hold_invoices: bool = False

    @abc.abstractmethod
    async def create_invoice(
        self, amount_sats: int, memo: str = "", expiry: int = 600
    ) -> Invoice: ...

    @abc.abstractmethod
    async def decode_invoice(self, payment_request: str) -> Invoice: ...

    @abc.abstractmethod
    async def pay_invoice(self, payment_request: str, max_fee_sats: int) -> PaymentResult:
        """Pay and return the preimage.

        Raises PaymentError when the payment definitely failed and PaymentPending
        when its outcome is unknown (it may still succeed).
        """

    @abc.abstractmethod
    async def is_invoice_settled(self, payment_hash: str) -> bool: ...

    @abc.abstractmethod
    async def get_balance_sats(self) -> int: ...

    async def invoice_state(self, payment_hash: str) -> InvoiceState:
        settled = await self.is_invoice_settled(payment_hash)
        return InvoiceState.SETTLED if settled else InvoiceState.OPEN

    async def payment_status(self, payment_hash: str) -> PaymentStatus:
        """Status of an outgoing payment, used to resolve payments left pending."""
        raise NotImplementedError(f"{type(self).__name__} cannot look up payments")

    async def create_hold_invoice(
        self, amount_sats: int, payment_hash: str, memo: str = "", expiry: int = 600
    ) -> Invoice:
        raise NotImplementedError(f"{type(self).__name__} does not support hold invoices")

    async def settle_hold_invoice(self, preimage: str) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support hold invoices")

    async def cancel_hold_invoice(self, payment_hash: str) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support hold invoices")

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release network resources."""
