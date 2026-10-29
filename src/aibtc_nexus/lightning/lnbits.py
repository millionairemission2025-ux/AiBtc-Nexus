"""LNbits backend (https://lnbits.com) using its wallet REST API."""

from __future__ import annotations

import httpx

from ..errors import BackendError, PaymentError
from .base import Invoice, LightningBackend, PaymentResult, preimage_matches


class LnbitsBackend(LightningBackend):
    """An LNbits wallet. The admin key is needed only to pay; the invoice key
    is enough for a provider that only receives.

    LNbits applies its own fee reserve; ``max_fee_sats`` is checked after the
    payment against the fee LNbits reports.
    """

    def __init__(
        self,
        url: str,
        invoice_key: str,
        admin_key: str | None = None,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.invoice_key = invoice_key
        self.admin_key = admin_key
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=timeout, transport=transport
        )

    async def _request(self, method: str, path: str, key: str, **kwargs) -> dict:
        try:
            resp = await self._client.request(method, path, headers={"X-Api-Key": key}, **kwargs)
        except httpx.HTTPError as e:
            raise BackendError(f"LNbits unreachable: {e}") from e
        if resp.status_code >= 400:
            raise BackendError(f"LNbits {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def create_invoice(self, amount_sats: int, memo: str = "", expiry: int = 600) -> Invoice:
        data = await self._request(
            "POST",
            "/api/v1/payments",
            self.invoice_key,
            json={"out": False, "amount": amount_sats, "memo": memo, "expiry": expiry},
        )
        pr = data.get("payment_request") or data.get("bolt11")
        if not pr:
            raise BackendError("LNbits did not return a payment request")
        return await self.decode_invoice(pr)

    async def decode_invoice(self, payment_request: str) -> Invoice:
        data = await self._request(
            "POST", "/api/v1/payments/decode", self.invoice_key, json={"data": payment_request}
        )
        return Invoice(
            payment_request=payment_request,
            payment_hash=data["payment_hash"],
            amount_sats=int(data.get("amount_msat", 0)) // 1000,
            created_at=int(data.get("date", 0)),
            expiry=int(data.get("expiry", 3600)),
            memo=data.get("description", "") or "",
            payee=data.get("payee", "") or "",
        )

    async def pay_invoice(self, payment_request: str, max_fee_sats: int) -> PaymentResult:
        if not self.admin_key:
            raise PaymentError("LNbits admin key required to pay")
        invoice = await self.decode_invoice(payment_request)
        try:
            await self._request(
                "POST",
                "/api/v1/payments",
                self.admin_key,
                json={"out": True, "bolt11": payment_request},
            )
        except BackendError as e:
            raise PaymentError(str(e)) from e
        status = await self._request(
            "GET", f"/api/v1/payments/{invoice.payment_hash}", self.admin_key
        )
        if not status.get("paid"):
            raise PaymentError("LNbits reports the payment as not completed")
        preimage = status.get("preimage") or status.get("details", {}).get("preimage", "")
        if not preimage_matches(preimage, invoice.payment_hash):
            raise PaymentError("LNbits returned a preimage that does not match the payment hash")
        fee_msat = abs(int(status.get("details", {}).get("fee", 0) or 0))
        fee_sats = -(-fee_msat // 1000)
        if fee_sats > max_fee_sats:
            # Already paid; surfaced so the caller can tighten LNbits' fee reserve.
            raise PaymentError(
                f"routing fee {fee_sats} exceeded limit {max_fee_sats} (payment sent)"
            )
        return PaymentResult(invoice.payment_hash, preimage, invoice.amount_sats, fee_sats)

    async def is_invoice_settled(self, payment_hash: str) -> bool:
        data = await self._request("GET", f"/api/v1/payments/{payment_hash}", self.invoice_key)
        return bool(data.get("paid"))

    async def get_balance_sats(self) -> int:
        data = await self._request("GET", "/api/v1/wallet", self.invoice_key)
        return int(data.get("balance", 0)) // 1000

    async def aclose(self) -> None:
        await self._client.aclose()
