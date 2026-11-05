"""Minimal Gemini REST client (v1beta) over httpx.

The API key travels in the ``x-goog-api-key`` header, never in the URL, so it cannot leak through
URL logging. Errors are mapped to typed exceptions carrying the retry hint from ``RetryInfo``.
"""

from __future__ import annotations

import re
from typing import Any

import httpx


class GeminiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimited(GeminiError):
    def __init__(self, message: str, retry_after: float, per_day: bool) -> None:
        super().__init__(message, 429)
        self.retry_after = retry_after
        self.per_day = per_day


class InvalidKey(GeminiError):
    pass


class Forbidden(GeminiError):
    pass


class ModelNotFound(GeminiError):
    pass


class ServerError(GeminiError):
    pass


class BadRequest(GeminiError):
    pass


_DURATION = re.compile(r"^\s*([\d.]+)s\s*$")


def _parse_duration(v: Any) -> float | None:
    m = _DURATION.match(str(v or ""))
    return float(m.group(1)) if m else None


def map_error(resp: httpx.Response) -> GeminiError:
    status = resp.status_code
    try:
        err = resp.json().get("error", {})
    except ValueError:
        err = {}
    message = str(err.get("message") or f"HTTP {status}")[:300]
    details = err.get("details") or []
    reasons = {str(d.get("reason") or "") for d in details if isinstance(d, dict)}
    if status == 429:
        retry = None
        per_day = False
        for d in details:
            t = str(d.get("@type") or "")
            if t.endswith("RetryInfo"):
                retry = _parse_duration(d.get("retryDelay"))
            if t.endswith("QuotaFailure"):
                for v in d.get("violations") or []:
                    if "PerDay" in str(v.get("quotaId") or ""):
                        per_day = True
        return RateLimited(message, retry if retry is not None else 60.0, per_day)
    if status in (400, 401) and (
        "API_KEY_INVALID" in reasons or "API key not valid" in message or status == 401
    ):
        return InvalidKey(message, status)
    if status == 403:
        if "API_KEY_INVALID" in reasons:
            return InvalidKey(message, status)
        return Forbidden(message, status)
    if status == 404:
        return ModelNotFound(message, status)
    if status >= 500:
        return ServerError(message, status)
    return BadRequest(message, status)


class GeminiClient:
    def __init__(
        self,
        base_url: str = "https://generativelanguage.googleapis.com",
        timeout: float = 45.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, key: str, **kw: Any) -> dict[str, Any]:
        try:
            resp = await self._client.request(
                method, f"{self.base_url}{path}", headers={"x-goog-api-key": key}, **kw
            )
        except httpx.HTTPError as exc:
            raise ServerError(f"network error: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise map_error(resp)
        return resp.json()

    async def list_models(self, key: str) -> list[dict[str, Any]]:
        models: list[dict[str, Any]] = []
        token: str | None = None
        for _ in range(20):
            params: dict[str, Any] = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            data = await self._request("GET", "/v1beta/models", key, params=params)
            models.extend(data.get("models", []))
            token = data.get("nextPageToken")
            if not token:
                break
        return models

    async def generate(self, key: str, model: str, body: dict[str, Any]) -> dict[str, Any]:
        name = model if model.startswith("models/") else f"models/{model}"
        return await self._request("POST", f"/v1beta/{name}:generateContent", key, json=body)


def response_parts(resp: dict[str, Any]) -> list[dict[str, Any]]:
    cands = resp.get("candidates") or []
    if not cands:
        return []
    return list((cands[0].get("content") or {}).get("parts") or [])


def response_text(resp: dict[str, Any]) -> str:
    return "".join(
        str(p.get("text") or "") for p in response_parts(resp) if not p.get("thought")
    ).strip()


def function_calls(resp: dict[str, Any]) -> list[dict[str, Any]]:
    return [p["functionCall"] for p in response_parts(resp) if "functionCall" in p]
