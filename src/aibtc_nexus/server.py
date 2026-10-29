"""HTTP transport (Starlette) for providers and for the fee-collecting platform.

Install with ``pip install aibtc-nexus[server]``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .errors import NexusError, ProtocolError
from .fees import FeeSettler
from .lightning.base import LightningBackend
from .provider import ProviderAgent, UnknownService

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ProtocolError("request body too large")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ProtocolError("JSON body must be an object")
    return data


def create_provider_app(
    provider: ProviderAgent,
    fee_settler: FeeSettler | None = None,
    settle_interval_seconds: float = 3600,
    admin_token: str | None = None,
) -> Starlette:
    """HTTP API for a provider. With ``fee_settler``, accrued platform fees are
    paid in the background every ``settle_interval_seconds``. With ``admin_token``,
    ``GET /v1/admin/stats`` reports sales to callers presenting it as a bearer token."""

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        task = None
        if fee_settler is not None:

            async def settle_loop() -> None:
                while True:
                    await asyncio.sleep(settle_interval_seconds)
                    try:
                        await fee_settler.settle()
                    except Exception:
                        log.exception("platform fee settlement failed; will retry")

            task = asyncio.create_task(settle_loop())
        try:
            yield
        finally:
            if task:
                task.cancel()

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def info(request: Request) -> JSONResponse:
        return JSONResponse(provider.info())

    async def services(request: Request) -> JSONResponse:
        return JSONResponse({"services": provider.catalog()})

    async def negotiate(request: Request) -> JSONResponse:
        try:
            reply = await provider.handle_negotiation(await _json_body(request))
        except UnknownService as e:
            return JSONResponse({"error": "not_found", "detail": str(e)}, status_code=404)
        except ProtocolError as e:
            return JSONResponse({"error": "bad_request", "detail": str(e)}, status_code=400)
        return JSONResponse(reply)

    async def stats(request: Request) -> JSONResponse:
        given = request.headers.get("Authorization", "")
        if not admin_token or not hmac.compare_digest(given, f"Bearer {admin_token}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(provider.stats())

    async def invoke(request: Request) -> JSONResponse:
        try:
            body = await _json_body(request)
        except ProtocolError as e:
            return JSONResponse({"error": "bad_request", "detail": str(e)}, status_code=400)
        params = body.get("params", {})
        if not isinstance(params, dict):
            return JSONResponse({"error": "bad_request", "detail": "params must be an object"}, 400)
        resp = await provider.invoke(
            request.path_params["service_id"],
            params,
            quote_id=request.headers.get("X-Nexus-Quote"),
            authorization=request.headers.get("Authorization"),
        )
        return JSONResponse(resp.body, status_code=resp.status, headers=resp.headers)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/v1/info", info),
            Route("/v1/services", services),
            Route("/v1/negotiate", negotiate, methods=["POST"]),
            Route("/v1/admin/stats", stats),
            Route("/v1/services/{service_id}/invoke", invoke, methods=["POST"]),
        ],
        lifespan=lifespan,
    )


def create_platform_app(backend: LightningBackend, max_invoice_sats: int = 10_000_000) -> Starlette:
    """The operator's endpoint where providers pay their accrued platform fees."""

    async def fee_invoice(request: Request) -> JSONResponse:
        try:
            body = await _json_body(request)
            amount = int(body["amount_sats"])
            provider_id = str(body.get("provider_id", ""))[:128]
        except (ProtocolError, KeyError, TypeError, ValueError) as e:
            return JSONResponse({"error": "bad_request", "detail": str(e)}, status_code=400)
        if not 0 < amount <= max_invoice_sats:
            return JSONResponse({"error": "bad_request", "detail": "amount out of range"}, 400)
        try:
            invoice = await backend.create_invoice(
                amount, memo=f"aibtc-nexus fees {provider_id[:16]}"
            )
        except NexusError as e:
            return JSONResponse({"error": "backend", "detail": str(e)}, status_code=502)
        return JSONResponse(
            {"payment_request": invoice.payment_request, "payment_hash": invoice.payment_hash}
        )

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/v1/platform/fee-invoice", fee_invoice, methods=["POST"]),
        ]
    )
