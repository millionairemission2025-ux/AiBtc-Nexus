"""LND backend using the REST API (https://lightning.engineering/api-docs/api/lnd/)."""

from __future__ import annotations

import base64
import json
import ssl

import httpx

from ..errors import BackendError, PaymentError, PaymentPending
from .base import (
    Invoice,
    InvoiceState,
    LightningBackend,
    PaymentResult,
    PaymentState,
    PaymentStatus,
    preimage_matches,
)

_INVOICE_STATES = {
    "OPEN": InvoiceState.OPEN,
    "ACCEPTED": InvoiceState.ACCEPTED,
    "SETTLED": InvoiceState.SETTLED,
    "CANCELED": InvoiceState.CANCELED,
}


def _b64(hex_value: str) -> str:
    """LND REST encodes ``bytes`` fields as base64 in JSON bodies."""
    return base64.b64encode(bytes.fromhex(hex_value)).decode()


def _b64url(hex_value: str) -> str:
    """…and as URL-safe base64 in URL paths."""
    return base64.urlsafe_b64encode(bytes.fromhex(hex_value)).decode()


class LndRestBackend(LightningBackend):
    """Talks to an LND node over REST, authenticated with a macaroon.

    Use a macaroon scoped to what the agent needs, never the admin macaroon of a
    node holding significant funds (see SECURITY.md).
    """

    supports_hold_invoices = True

    def __init__(
        self,
        url: str,
        macaroon_hex: str,
        tls_cert_path: str | None = None,
        timeout: float = 60.0,
        payment_timeout_seconds: int = 60,
        payment_wait_seconds: float = 900.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """``payment_timeout_seconds`` bounds route finding; ``payment_wait_seconds``
        bounds how long a payment may stay locked, e.g. while an escrow provider
        runs its service before settling."""
        self.payment_timeout_seconds = payment_timeout_seconds
        self.payment_wait_seconds = payment_wait_seconds
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Grpc-Metadata-macaroon": macaroon_hex},
            verify=ssl.create_default_context(cafile=tls_cert_path) if tls_cert_path else True,
            timeout=timeout,
            transport=transport,
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise BackendError(f"LND unreachable: {e}") from e
        if resp.status_code >= 400:
            raise BackendError(f"LND {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def create_invoice(self, amount_sats: int, memo: str = "", expiry: int = 600) -> Invoice:
        data = await self._request(
            "POST",
            "/v1/invoices",
            json={"value": str(amount_sats), "memo": memo, "expiry": str(expiry)},
        )
        # Decode the invoice we just made so every field comes from the node itself.
        return await self.decode_invoice(data["payment_request"])

    async def create_hold_invoice(
        self, amount_sats: int, payment_hash: str, memo: str = "", expiry: int = 600
    ) -> Invoice:
        data = await self._request(
            "POST",
            "/v2/invoices/hodl",
            json={
                "hash": _b64(payment_hash),
                "value": str(amount_sats),
                "memo": memo,
                "expiry": str(expiry),
            },
        )
        return await self.decode_invoice(data["payment_request"])

    async def settle_hold_invoice(self, preimage: str) -> None:
        await self._request("POST", "/v2/invoices/settle", json={"preimage": _b64(preimage)})

    async def cancel_hold_invoice(self, payment_hash: str) -> None:
        await self._request(
            "POST", "/v2/invoices/cancel", json={"payment_hash": _b64(payment_hash)}
        )

    async def decode_invoice(self, payment_request: str) -> Invoice:
        data = await self._request("GET", f"/v1/payreq/{payment_request}")
        return Invoice(
            payment_request=payment_request,
            payment_hash=data["payment_hash"],
            amount_sats=int(data.get("num_satoshis", 0)),
            created_at=int(data.get("timestamp", 0)),
            expiry=int(data.get("expiry", 3600)),
            memo=data.get("description", ""),
            payee=data.get("destination", ""),
        )

    async def _router_stream(self, method: str, path: str, **kwargs) -> dict | None:
        """Read a router stream and return its last ``Payment`` message."""
        final: dict | None = None
        async with self._client.stream(method, path, **kwargs) as resp:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode(errors="replace")
                raise PaymentError(f"LND rejected the request ({resp.status_code}): {text[:300]}")
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                msg = json.loads(line)
                if "error" in msg:
                    raise PaymentError(f"LND error: {msg['error']}")
                final = msg.get("result", msg)
                if final.get("status") in ("SUCCEEDED", "FAILED"):
                    break
        return final

    async def pay_invoice(self, payment_request: str, max_fee_sats: int) -> PaymentResult:
        body = {
            "payment_request": payment_request,
            "timeout_seconds": self.payment_timeout_seconds,
            "fee_limit_sat": str(max_fee_sats),
            "no_inflight_updates": True,
        }
        try:
            final = await self._router_stream(
                "POST",
                "/v2/router/send",
                json=body,
                timeout=httpx.Timeout(self._timeout, read=self.payment_wait_seconds),
            )
        except httpx.HTTPError as e:
            # The request may have reached the node: the payment could be in flight.
            raise PaymentPending(f"LND connection lost during payment: {e}") from e
        if not final:
            raise PaymentPending("LND closed the payment stream without a result")
        status = final.get("status")
        if status == "FAILED":
            raise PaymentError(f"payment failed: {final.get('failure_reason', 'unknown')}")
        if status != "SUCCEEDED":
            raise PaymentPending(f"payment still {status}")
        payment_hash = final["payment_hash"]
        preimage = final["payment_preimage"]
        if not preimage_matches(preimage, payment_hash):
            raise PaymentError("LND returned a preimage that does not match the payment hash")
        return PaymentResult(
            payment_hash=payment_hash,
            preimage=preimage,
            amount_sats=int(final.get("value_sat", 0)),
            fee_sats=int(final.get("fee_sat", 0)),
        )

    async def payment_status(self, payment_hash: str) -> PaymentStatus:
        try:
            final = await self._router_stream(
                "GET",
                f"/v2/router/track/{_b64url(payment_hash)}",
                params={"no_inflight_updates": "false"},
                timeout=10.0,
            )
        except PaymentError as e:
            # LND: "payment isn't initiated" when it never attempted this hash.
            if "initiated" in str(e) or "not found" in str(e).lower():
                return PaymentStatus(PaymentState.NOT_FOUND)
            raise BackendError(str(e)) from e
        except httpx.ReadTimeout:
            return PaymentStatus(PaymentState.PENDING)
        except httpx.HTTPError as e:
            raise BackendError(f"LND unreachable: {e}") from e
        status = (final or {}).get("status")
        if status == "SUCCEEDED":
            return PaymentStatus(
                PaymentState.SUCCEEDED, final["payment_preimage"], int(final.get("fee_sat", 0))
            )
        if status == "FAILED":
            return PaymentStatus(PaymentState.FAILED)
        return PaymentStatus(PaymentState.PENDING)

    async def invoice_state(self, payment_hash: str) -> InvoiceState:
        data = await self._request("GET", f"/v1/invoice/{payment_hash}")
        return _INVOICE_STATES.get(data.get("state", ""), InvoiceState.OPEN)

    async def is_invoice_settled(self, payment_hash: str) -> bool:
        return await self.invoice_state(payment_hash) is InvoiceState.SETTLED

    async def get_balance_sats(self) -> int:
        data = await self._request("GET", "/v1/balance/channels")
        return int(data.get("local_balance", {}).get("sat", 0))

    async def aclose(self) -> None:
        await self._client.aclose()
