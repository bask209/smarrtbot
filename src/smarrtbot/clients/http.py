"""Shared HTTP plumbing: retries with backoff, short error messages, no secrets in errors."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Failures where the request certainly never reached the service: safe to retry any method.
_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# Failures after the request may have been processed: retry only idempotent methods.
_READ_ERRORS = (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError)
_IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}


class ServiceError(Exception):
    def __init__(self, service: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service
        self.status = status


class ServiceUnavailable(ServiceError):
    pass


class HttpService:
    name = "service"

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 1.0,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        self._headers = headers or {}
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}{path}"
        method = method.upper()
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = await self._client.request(
                    method, url, params=params, json=json, data=data, headers=self._headers
                )
            except _CONNECT_ERRORS as exc:
                last = exc
            except _READ_ERRORS as exc:
                if method not in _IDEMPOTENT:
                    raise ServiceUnavailable(
                        self.name, f"{type(exc).__name__} on {method}"
                    ) from None
                last = exc
            else:
                if resp.status_code in (502, 503, 504) and attempt < self._retries:
                    last = ServiceError(self.name, f"HTTP {resp.status_code}", resp.status_code)
                else:
                    return resp
            if attempt < self._retries:
                delay = self._backoff * (2**attempt)
                log.debug("%s: %s, retrying in %.1fs", self.name, type(last).__name__, delay)
                await self._sleep(delay)
        raise ServiceUnavailable(self.name, f"unreachable ({type(last).__name__})") from None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        resp = await self._send(method, path, params=params, json=json, data=data)
        if resp.status_code >= 400:
            raise ServiceError(self.name, _short_error(resp), resp.status_code)
        if not resp.content:
            return None
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            return resp.json()
        return resp.text

    async def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return await self.request("GET", path, params=clean or None)


def _short_error(resp: httpx.Response) -> str:
    text = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            text = str(body.get("message") or body.get("error") or "")
        elif isinstance(body, list) and body and isinstance(body[0], dict):
            text = str(body[0].get("errorMessage") or body[0].get("message") or "")
    except ValueError:
        text = resp.text[:200]
    return f"HTTP {resp.status_code}" + (f": {text[:200]}" if text else "")
