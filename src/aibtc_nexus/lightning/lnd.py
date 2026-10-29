"""LND backend using the REST API (https://lightning.engineering/api-docs/api/lnd/)."""

from __future__ import annotations

import json

import httpx

from ..errors import BackendError, PaymentError
from .base import Invoice, LightningBackend, PaymentResult, preimage_matches


class LndRestBackend(LightningBackend):
    """Talks to an LND node over REST, authenticated with a macaroon.

    Use an invoice+offchain macaroon scoped to what the agent needs, never the
    admin macaroon of a node holding significant funds.
    """

    def __init__(
        self,
        url: str,
        macaroon_hex: str,
        tls_cert_path: str | None = None,
        timeout: float = 60.0,
        payment_timeout_seconds: int = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.payment_timeout_seconds = payment_timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Grpc-Metadata-macaroon": macaroon_hex},
            verify=tls_cert_path if tls_cert_path else True,
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

    async def pay_invoice(self, payment_request: str, max_fee_sats: int) -> PaymentResult:
        body = {
            "payment_request": payment_request,
            "timeout_seconds": self.payment_timeout_seconds,
            "fee_limit_sat": str(max_fee_sats),
            "no_inflight_updates": True,
        }
        final: dict | None = None
        try:
            async with self._client.stream("POST", "/v2/router/send", json=body) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode(errors="replace")
                    raise PaymentError(f"LND payment rejected ({resp.status_code}): {text[:300]}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    if "error" in msg:
                        raise PaymentError(f"LND payment error: {msg['error']}")
                    final = msg.get("result", msg)
        except httpx.HTTPError as e:
            raise BackendError(f"LND unreachable: {e}") from e
        if not final:
            raise PaymentError("LND returned no payment result")
        if final.get("status") != "SUCCEEDED":
            raise PaymentError(
                f"payment failed: {final.get('failure_reason', final.get('status'))}"
            )
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

    async def is_invoice_settled(self, payment_hash: str) -> bool:
        data = await self._request("GET", f"/v1/invoice/{payment_hash}")
        return data.get("state") == "SETTLED"

    async def get_balance_sats(self) -> int:
        data = await self._request("GET", "/v1/balance/channels")
        return int(data.get("local_balance", {}).get("sat", 0))

    async def aclose(self) -> None:
        await self._client.aclose()
