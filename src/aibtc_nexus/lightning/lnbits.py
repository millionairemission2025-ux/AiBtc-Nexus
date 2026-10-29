"""LNbits backend (https://lnbits.com) using its wallet REST API."""

from __future__ import annotations

import httpx

from ..errors import BackendError, PaymentError, PaymentPending
from .base import (
    Invoice,
    LightningBackend,
    PaymentResult,
    PaymentState,
    PaymentStatus,
    preimage_matches,
)


class _HttpStatusError(BackendError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


class LnbitsBackend(LightningBackend):
    """An LNbits wallet. The admin key is needed only to pay; the invoice key
    is enough for a provider that only receives.

    LNbits applies its own fee reserve, so ``max_fee_sats`` cannot be enforced
    per payment; a fee above it is logged by the caller, not treated as a failure
    (the payment has already gone through).
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
            raise _HttpStatusError(
                resp.status_code,
                f"LNbits {method} {path} -> {resp.status_code}: {resp.text[:300]}",
            )
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
        except _HttpStatusError as e:
            if e.status_code >= 500:
                raise PaymentPending(str(e)) from e
            raise PaymentError(str(e)) from e
        except BackendError as e:  # connection lost: the payment may be in flight
            raise PaymentPending(str(e)) from e
        status = await self.payment_status(invoice.payment_hash)
        if status.state is PaymentState.FAILED:
            raise PaymentError("LNbits reports the payment as failed")
        if status.state is not PaymentState.SUCCEEDED:
            raise PaymentPending("LNbits reports the payment as not completed yet")
        if not preimage_matches(status.preimage, invoice.payment_hash):
            raise PaymentError("LNbits returned a preimage that does not match the payment hash")
        return PaymentResult(
            invoice.payment_hash, status.preimage, invoice.amount_sats, status.fee_sats
        )

    async def payment_status(self, payment_hash: str) -> PaymentStatus:
        try:
            data = await self._request(
                "GET", f"/api/v1/payments/{payment_hash}", self.admin_key or self.invoice_key
            )
        except _HttpStatusError as e:
            if e.status_code == 404:
                return PaymentStatus(PaymentState.NOT_FOUND)
            raise
        details = data.get("details") or {}
        if data.get("paid"):
            preimage = data.get("preimage") or details.get("preimage", "")
            fee_msat = abs(int(details.get("fee", 0) or 0))
            return PaymentStatus(PaymentState.SUCCEEDED, preimage, -(-fee_msat // 1000))
        if str(data.get("status") or details.get("status", "")).lower() == "failed":
            return PaymentStatus(PaymentState.FAILED)
        return PaymentStatus(PaymentState.PENDING)

    async def is_invoice_settled(self, payment_hash: str) -> bool:
        data = await self._request("GET", f"/api/v1/payments/{payment_hash}", self.invoice_key)
        return bool(data.get("paid"))

    async def get_balance_sats(self) -> int:
        data = await self._request("GET", "/api/v1/wallet", self.invoice_key)
        return int(data.get("balance", 0)) // 1000

    async def aclose(self) -> None:
        await self._client.aclose()
