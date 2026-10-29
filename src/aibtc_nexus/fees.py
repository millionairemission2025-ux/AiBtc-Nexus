"""Platform fees: how an operator of AiBtc-Nexus earns revenue.

A provider that uses the hosted platform (or any operator's) accrues a fee on
every settled sale. Fees are batched and paid to the platform by Lightning once
they exceed a threshold, so tiny per-call fees don't each cost a routing fee.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from .errors import PaymentError
from .ledger import Ledger
from .lightning.base import LightningBackend

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeeSchedule:
    basis_points: int = 100  # 1%
    min_sats: int = 0
    max_sats: int | None = None

    def fee_for(self, amount_sats: int) -> int:
        fee = -(-amount_sats * self.basis_points // 10_000)  # round up
        fee = max(fee, self.min_sats)
        if self.max_sats is not None:
            fee = min(fee, self.max_sats)
        return min(fee, amount_sats)


InvoiceFetcher = Callable[[int, str], Awaitable[str]]


def http_invoice_fetcher(platform_url: str, timeout: float = 30.0) -> InvoiceFetcher:
    """Ask a platform server (see ``server.create_platform_app``) for a fee invoice."""

    async def fetch(amount_sats: int, provider_id: str) -> str:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                platform_url.rstrip("/") + "/v1/platform/fee-invoice",
                json={"amount_sats": amount_sats, "provider_id": provider_id},
            )
            resp.raise_for_status()
            return resp.json()["payment_request"]

    return fetch


class FeeSettler:
    def __init__(
        self,
        ledger: Ledger,
        backend: LightningBackend,
        fetch_invoice: InvoiceFetcher,
        provider_id: str,
        threshold_sats: int = 1_000,
        max_routing_fee_sats: int = 20,
    ):
        self.ledger = ledger
        self.backend = backend
        self.fetch_invoice = fetch_invoice
        self.provider_id = provider_id
        self.threshold_sats = threshold_sats
        self.max_routing_fee_sats = max_routing_fee_sats

    async def settle(self, force: bool = False) -> int:
        """Pay accrued fees if over the threshold. Returns sats paid (0 if nothing)."""
        amount, hashes = self.ledger.accrued_fees()
        if amount <= 0 or (amount < self.threshold_sats and not force):
            return 0
        pr = await self.fetch_invoice(amount, self.provider_id)
        invoice = await self.backend.decode_invoice(pr)
        if invoice.amount_sats != amount:
            raise PaymentError(f"platform invoice for {invoice.amount_sats}, expected {amount}")
        result = await self.backend.pay_invoice(pr, self.max_routing_fee_sats)
        self.ledger.mark_fees_settled(hashes, result.payment_hash)
        log.info("settled %s sats of platform fees", amount)
        return amount
