"""HTTP transport (Starlette) for providers and for the fee-collecting platform.

Install with ``pip install aibtc-nexus[server]``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .errors import NexusError, ProtocolError
from .fees import FeeSettler
from .lightning.base import LightningBackend
from .provider import ProviderAgent, UnknownService

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024


class _BodyTooLarge(ProtocolError):
    pass


async def _json_body(request: Request) -> dict:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _BodyTooLarge("request body too large")
    raw = bytearray()
    async for chunk in request.stream():  # stop reading as soon as the limit is passed
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            raise _BodyTooLarge("request body too large")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ProtocolError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ProtocolError("JSON body must be an object")
    return data


def _bad_request(e: ProtocolError) -> JSONResponse:
    status = 413 if isinstance(e, _BodyTooLarge) else 400
    return JSONResponse({"error": "bad_request", "detail": str(e)}, status_code=status)


class RateLimitMiddleware:
    """Token bucket per client IP for POST requests (negotiation and invoices are
    the expensive calls). Set ``trust_proxy`` only behind a proxy that overwrites
    ``X-Forwarded-For``."""

    def __init__(
        self,
        app: ASGIApp,
        rate_per_second: float = 5.0,
        burst: int = 20,
        trust_proxy: bool = False,
        max_clients: int = 100_000,
    ):
        self.app = app
        self.rate = rate_per_second
        self.burst = burst
        self.trust_proxy = trust_proxy
        self.max_clients = max_clients
        self._buckets: dict[str, tuple[float, float]] = {}

    def _client(self, scope: Scope) -> str:
        if self.trust_proxy:
            for name, value in scope.get("headers", []):
                if name == b"x-forwarded-for":
                    return value.decode(errors="replace").split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if len(self._buckets) >= self.max_clients and key not in self._buckets:
            self._buckets.clear()  # crude but bounded memory under a flood of IPs
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        return True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            if not self.allow(self._client(scope)):
                response = JSONResponse(
                    {"error": "rate_limited", "detail": "slow down"},
                    status_code=429,
                    headers={"Retry-After": str(max(1, int(1 / self.rate)))},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_provider_app(
    provider: ProviderAgent,
    fee_settler: FeeSettler | None = None,
    settle_interval_seconds: float = 3600,
    admin_token: str | None = None,
    rate_limit_per_second: float | None = 5.0,
    rate_limit_burst: int = 20,
    trust_proxy: bool = False,
) -> Starlette:
    """HTTP API for a provider.

    - ``fee_settler``: pay accrued platform fees every ``settle_interval_seconds``.
    - ``admin_token``: enables ``GET /v1/admin/stats`` for that bearer token.
    - ``rate_limit_per_second``: per-IP limit on POST requests (None disables it).
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        await provider.start()  # resume escrow jobs from a previous run
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
            await provider.aclose()

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
            return _bad_request(e)
        return JSONResponse(reply)

    async def stats(request: Request) -> JSONResponse:
        given = request.headers.get("Authorization", "")
        if not admin_token or not hmac.compare_digest(given, f"Bearer {admin_token}"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(provider.stats())

    async def escrow(request: Request) -> JSONResponse:
        payment_hash = request.path_params["payment_hash"]
        status = provider.escrow_status(payment_hash) if len(payment_hash) == 64 else None
        if status is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(status)

    async def invoke(request: Request) -> JSONResponse:
        try:
            body = await _json_body(request)
        except ProtocolError as e:
            return _bad_request(e)
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

    app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/v1/info", info),
            Route("/v1/services", services),
            Route("/v1/negotiate", negotiate, methods=["POST"]),
            Route("/v1/admin/stats", stats),
            Route("/v1/escrow/{payment_hash}", escrow),
            Route("/v1/services/{service_id}/invoke", invoke, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    if rate_limit_per_second:
        app.add_middleware(
            RateLimitMiddleware,
            rate_per_second=rate_limit_per_second,
            burst=rate_limit_burst,
            trust_proxy=trust_proxy,
        )
    return app


def create_platform_app(backend: LightningBackend, max_invoice_sats: int = 10_000_000) -> Starlette:
    """The operator's endpoint where providers pay their accrued platform fees."""

    async def fee_invoice(request: Request) -> JSONResponse:
        try:
            body = await _json_body(request)
            amount = int(body["amount_sats"])
            provider_id = str(body.get("provider_id", ""))[:128]
        except ProtocolError as e:
            return _bad_request(e)
        except (KeyError, TypeError, ValueError) as e:
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

    app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/v1/platform/fee-invoice", fee_invoice, methods=["POST"]),
        ]
    )
    app.add_middleware(RateLimitMiddleware, rate_per_second=2.0, burst=10)
    return app
