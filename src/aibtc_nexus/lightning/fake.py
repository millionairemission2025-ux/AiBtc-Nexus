"""In-memory Lightning network for tests, demos and local development.

Payment hashes and preimages behave like real Lightning (hash = sha256(preimage)),
but no bitcoin moves. Invoices are encoded as ``lnfake1<base64 json>``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from ..errors import BackendError, PaymentError
from .base import Invoice, LightningBackend, PaymentResult

PREFIX = "lnfake1"


@dataclass
class _InvoiceRecord:
    invoice: Invoice
    preimage: str
    settled: bool = False


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
    def __init__(self, network: FakeLightningNetwork, name: str, balance_sats: int = 0):
        self.network = network
        self.name = name
        self.balance_sats = balance_sats
        self._invoices: dict[str, _InvoiceRecord] = {}

    async def create_invoice(self, amount_sats: int, memo: str = "", expiry: int = 600) -> Invoice:
        if amount_sats <= 0:
            raise BackendError("amount must be positive")
        preimage = os.urandom(32).hex()
        payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
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
        invoice = Invoice(pr, payment_hash, amount_sats, created, expiry, memo, self.name)
        self._invoices[payment_hash] = _InvoiceRecord(invoice, preimage)
        return invoice

    async def decode_invoice(self, payment_request: str) -> Invoice:
        return decode_fake_invoice(payment_request)

    async def pay_invoice(self, payment_request: str, max_fee_sats: int) -> PaymentResult:
        invoice = decode_fake_invoice(payment_request)
        payee = self.network.nodes.get(invoice.payee)
        if payee is None:
            raise PaymentError("no route to payee")
        record = payee._invoices.get(invoice.payment_hash)
        if record is None:
            raise PaymentError("unknown invoice")
        if record.settled:
            raise PaymentError("invoice already paid")
        if invoice.is_expired():
            raise PaymentError("invoice expired")
        fee = 0 if payee is self else self.network.routing_fee_sats
        if fee > max_fee_sats:
            raise PaymentError(f"routing fee {fee} exceeds limit {max_fee_sats}")
        total = invoice.amount_sats + fee
        if self.balance_sats < total:
            raise PaymentError("insufficient balance")
        self.balance_sats -= total
        payee.balance_sats += invoice.amount_sats
        record.settled = True
        return PaymentResult(invoice.payment_hash, record.preimage, invoice.amount_sats, fee)

    async def is_invoice_settled(self, payment_hash: str) -> bool:
        record = self._invoices.get(payment_hash)
        return bool(record and record.settled)

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
