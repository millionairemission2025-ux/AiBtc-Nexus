"""Transports a buyer uses to reach a provider."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from .errors import ProtocolError
from .provider import HttpLikeResponse, ProviderAgent, UnknownService


class ProviderClient(Protocol):
    async def info(self) -> dict: ...

    async def services(self) -> list[dict]: ...

    async def negotiate(self, envelope: dict) -> dict: ...

    async def invoke(
        self,
        service_id: str,
        params: dict,
        quote_id: str | None = None,
        authorization: str | None = None,
    ) -> HttpLikeResponse: ...


class HttpProviderClient:
    """Talks to a provider over HTTP. Pass ``transport`` to target an ASGI app in tests."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> HttpProviderClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError as e:
            raise ProtocolError(f"provider returned non-JSON ({resp.status_code})") from e
        if not isinstance(data, dict):
            raise ProtocolError("provider returned a non-object JSON body")
        return data

    async def info(self) -> dict:
        resp = await self._client.get("/v1/info")
        resp.raise_for_status()
        return self._json(resp)

    async def services(self) -> list[dict]:
        resp = await self._client.get("/v1/services")
        resp.raise_for_status()
        return list(self._json(resp).get("services", []))

    async def negotiate(self, envelope: dict) -> dict:
        resp = await self._client.post("/v1/negotiate", json=envelope)
        data = self._json(resp)
        if resp.status_code != 200:
            raise ProtocolError(f"provider refused negotiation message: {data.get('detail', data)}")
        return data

    async def invoke(
        self,
        service_id: str,
        params: dict,
        quote_id: str | None = None,
        authorization: str | None = None,
    ) -> HttpLikeResponse:
        headers = {}
        if quote_id:
            headers["X-Nexus-Quote"] = quote_id
        if authorization:
            headers["Authorization"] = authorization
        resp = await self._client.post(
            f"/v1/services/{service_id}/invoke", json={"params": params}, headers=headers
        )
        return HttpLikeResponse(resp.status_code, self._json(resp), dict(resp.headers))


class LocalProviderClient:
    """Calls a ProviderAgent in the same process (no HTTP)."""

    def __init__(self, provider: ProviderAgent):
        self.provider = provider

    async def info(self) -> dict:
        return self.provider.info()

    async def services(self) -> list[dict]:
        return self.provider.catalog()

    async def negotiate(self, envelope: dict) -> dict:
        try:
            return await self.provider.handle_negotiation(envelope)
        except UnknownService as e:
            raise ProtocolError(str(e)) from e

    async def invoke(
        self,
        service_id: str,
        params: dict,
        quote_id: str | None = None,
        authorization: str | None = None,
    ) -> HttpLikeResponse:
        return await self.provider.invoke(service_id, params, quote_id, authorization)
