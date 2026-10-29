"""Ready-made service handlers."""

from __future__ import annotations

from typing import Any

import httpx

from .errors import ServiceError


async def echo_handler(params: dict) -> Any:
    return {"echo": params}


class HttpProxyHandler:
    """Forward the buyer's params to an upstream HTTP API and return its JSON.

    This is how an existing API is put behind a Lightning paywall without
    changing it: the provider agent charges, the upstream does the work.
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 30.0):
        self.url = url
        self.headers = headers or {}
        self.timeout = timeout

    async def __call__(self, params: dict) -> Any:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(self.url, json=params, headers=self.headers)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError) as e:
                raise ServiceError(f"upstream error: {e}") from e


def handler_from_spec(spec: str | dict) -> Any:
    """``"echo"`` or ``{"type": "http", "url": "...", "headers": {...}}``."""
    if spec == "echo" or spec == {"type": "echo"}:
        return echo_handler
    if isinstance(spec, dict) and spec.get("type") == "http":
        return HttpProxyHandler(spec["url"], spec.get("headers"), float(spec.get("timeout", 30)))
    raise ValueError(f"unknown handler spec: {spec!r}")
