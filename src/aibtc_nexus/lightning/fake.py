"""In-memory Lightning network for tests, demos and local development.

Payment hashes and preimages behave like real Lightning (hash = sha256(preimage)),
hold invoices lock the payer's funds until the payee settles or cancels, but no
bitcoin moves. Invoices are encoded as ``lnfake1<base64 json>``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from ..errors import BackendError, PaymentError
from .base import (
    Invoice,
    InvoiceState,
    LightningBackend,
    PaymentResult,
    PaymentState,
    PaymentStatus,
)

PREFIX = "lnfake1"


@dataclass
class _InvoiceRecord:
    invoice: Invoice
    preimage: str | None  # None for a hold invoice until it is settled
    state: InvoiceState = InvoiceState.OPEN
    hold: bool = False
    waiter: asyncio.Future | None = None
    payer: FakeLightningNode | None = None
    locked_sats: int = 0


@dataclass
class FakeLightningNetwork:
    """Routes payments between FakeLightningNode instances."""

    routing_fee_sats: int = 1
    nodes: dict[str, FakeLightningNode] = field(default_factory=dict)

    def create_node(self, name: str, balance_sats: int = 0) -> FakeLightningNode:
        if name in self.nodes:
            raise ValueError(f"node {name!r} already exists")
        node = FakeLightningNode(self, name, balance_sats)
        self.nodes[name] = node
        return node


class FakeLightningNode(LightningBackend):
    supports_hold_invoices = True

    def __init__(self, network: FakeLightningNetwork, name: str, balance_sats: int = 0):
        self.network = network
        self.name = name
        self.balance_sats = balance_sats
        self._invoices: dict[str, _InvoiceRecord] = {}
        self._payments: dict[str, PaymentStatus] = {}

    def _encode(self, payment_hash: str, amount_sats: int, memo: str, expiry: int) -> Invoice:
        if amount_sats <= 0:
            raise BackendError("amount must be positive")
        created = int(time.time())
        body = {
            "node": self.name,
            "hash": payment_hash,
            "amount": amount_sats,
            "memo": memo,
            "created": created,
            "expiry": expiry,
        }
        pr = PREFIX + base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
        return Invoice(pr, payment_hash, amount_sats, created, expiry, memo, self.name)

    async def create_invoice(self, amount_sats: int, memo: str = "", expiry: int = 600) -> Invoice:
        preimage = os.urandom(32).hex()
        payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        invoice = self._encode(payment_hash, amount_sats, memo, expiry)
        self._invoices[payment_hash] = _InvoiceRecord(invoice, preimage)
        return invoice

    async def create_hold_invoice(
        self, amount_sats: int, payment_hash: str, memo: str = "", expiry: int = 600
    ) -> Invoice:
        if payment_hash in self._invoices:
            raise BackendError("invoice with this hash already exists")
        invoice = self._encode(payment_hash, amount_sats, memo, expiry)
        self._invoices[payment_hash] = _InvoiceRecord(invoice, None, hold=True)
        return invoice

    async def decode_invoice(self, payment_request: str) -> Invoice:
        return decode_fake_invoice(payment_request)

    async def pay_invoice(self, payment_request: str, max_fee_sats: int) -> PaymentResult:
        invoice = decode_fake_invoice(payment_request)
        try:
            result = await self._pay(invoice, max_fee_sats)
        except PaymentError:
            self._payments[invoice.payment_hash] = PaymentStatus(PaymentState.FAILED)
            raise
        self._payments[invoice.payment_hash] = PaymentStatus(
            PaymentState.SUCCEEDED, result.preimage, result.fee_sats
        )
        return result

    async def _pay(self, invoice: Invoice, max_fee_sats: int) -> PaymentResult:
        payee = self.network.nodes.get(invoice.payee)
        if payee is None:
            raise PaymentError("no route to payee")
        record = payee._invoices.get(invoice.payment_hash)
        if record is None:
            raise PaymentError("unknown invoice")
        if record.state is not InvoiceState.OPEN:
            raise PaymentError(f"invoice already {record.state.value}")
        if invoice.is_expired():
            raise PaymentError("invoice expired")
        fee = 0 if payee is self else self.network.routing_fee_sats
        if fee > max_fee_sats:
            raise PaymentError(f"routing fee {fee} exceeds limit {max_fee_sats}")
        total = invoice.amount_sats + fee
        if self.balance_sats < total:
            raise PaymentError("insufficient balance")
        self.balance_sats -= total
        self._payments[invoice.payment_hash] = PaymentStatus(PaymentState.PENDING)

        if record.hold:
            record.state = InvoiceState.ACCEPTED
            record.payer, record.locked_sats = self, total
            record.waiter = asyncio.get_running_loop().create_future()
            preimage = await record.waiter  # raises PaymentError when canceled
            return PaymentResult(invoice.payment_hash, preimage, invoice.amount_sats, fee)

        payee.balance_sats += invoice.amount_sats
        record.state = InvoiceState.SETTLED
        return PaymentResult(invoice.payment_hash, record.preimage, invoice.amount_sats, fee)

    async def settle_hold_invoice(self, preimage: str) -> None:
        payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        record = self._invoices.get(payment_hash)
        if record is None or record.state is not InvoiceState.ACCEPTED:
            raise BackendError("no accepted hold invoice for this preimage")
        record.preimage = preimage
        record.state = InvoiceState.SETTLED
        self.balance_sats += record.invoice.amount_sats
        if record.waiter and not record.waiter.done():
            record.waiter.set_result(preimage)

    async def cancel_hold_invoice(self, payment_hash: str) -> None:
        record = self._invoices.get(payment_hash)
        if record is None or record.state is InvoiceState.SETTLED:
            raise BackendError("cannot cancel: unknown or settled invoice")
        if record.state is InvoiceState.ACCEPTED and record.payer is not None:
            record.payer.balance_sats += record.locked_sats  # HTLC fails back: refund
        record.state = InvoiceState.CANCELED
        if record.waiter and not record.waiter.done():
            record.waiter.set_exception(PaymentError("payment canceled by the payee"))

    async def invoice_state(self, payment_hash: str) -> InvoiceState:
        record = self._invoices.get(payment_hash)
        if record is None:
            raise BackendError("unknown invoice")
        return record.state

    async def is_invoice_settled(self, payment_hash: str) -> bool:
        record = self._invoices.get(payment_hash)
        return bool(record and record.state is InvoiceState.SETTLED)

    async def payment_status(self, payment_hash: str) -> PaymentStatus:
        return self._payments.get(payment_hash, PaymentStatus(PaymentState.NOT_FOUND))

    async def get_balance_sats(self) -> int:
        return self.balance_sats


def decode_fake_invoice(payment_request: str) -> Invoice:
    if not payment_request.startswith(PREFIX):
        raise BackendError("not a fake invoice")
    data = payment_request[len(PREFIX) :]
    try:
        body = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        return Invoice(
            payment_request,
            body["hash"],
            int(body["amount"]),
            int(body["created"]),
            int(body["expiry"]),
            body.get("memo", ""),
            body["node"],
        )
    except (ValueError, KeyError, TypeError) as e:
        raise BackendError(f"malformed fake invoice: {e}") from e
