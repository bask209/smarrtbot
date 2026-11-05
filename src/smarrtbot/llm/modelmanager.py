"""Discover, rank, test and track Gemini models.

The API does not say which models are free, so we learn it: a 403 on every key marks a model
``notfree``, repeated failures mark it ``dead``, a 404 marks it ``gone`` and triggers
re-discovery. The daily job revives dead models and re-tests the best candidate with a tiny
function-calling prompt. A config pin overrides the ranking while the pinned model works.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from smarrtbot.llm.gemini import (
    Forbidden,
    GeminiError,
    InvalidKey,
    ModelNotFound,
    RateLimited,
    function_calls,
)

log = logging.getLogger(__name__)

_EXCLUDE = re.compile(
    r"(embed|imagen|veo|tts|image|audio|live|aqa|learnlm|robotics|computer-use|gemma|nano|banana)"
)
_VERSION = re.compile(r"gemini-(\d+(?:\.\d+)?)")
USABLE = ("ok", "untested")

PING_TOOL = {
    "functionDeclarations": [
        {
            "name": "ping",
            "description": "Health check. Always call this with value 'pong'.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        }
    ]
}


def short(name: str) -> str:
    return name.removeprefix("models/")


def is_candidate(model: dict[str, Any]) -> bool:
    name = short(str(model.get("name") or ""))
    methods = model.get("supportedGenerationMethods") or []
    return name.startswith("gemini") and "generateContent" in methods and not _EXCLUDE.search(name)


def rank_key(name: str) -> tuple[int, float, int, int]:
    n = short(name)
    preview = "preview" in n or "-exp" in n or "experimental" in n
    lite = "flash-lite" in n
    flash = "flash" in n and not lite
    pro = "pro" in n
    m = _VERSION.search(n)
    version = float(m.group(1)) if m else 0.0
    if flash and not preview:
        tier = 0
    elif lite and not preview:
        tier = 1
    elif (flash or lite) and preview:
        tier = 2
    elif pro and not preview:
        tier = 4
    elif pro:
        tier = 5
    else:
        tier = 3
    alias = 1 if n.endswith("-latest") else 0
    dated = 1 if re.search(r"-\d{3}$|-\d{2}-\d{4}$", n) else 0
    return (tier, -version, alias, dated)


def rank(names: list[str]) -> list[str]:
    return sorted({short(n) for n in names}, key=lambda n: (rank_key(n), n))


class ModelManager:
    def __init__(
        self,
        gemini: Any,
        keypool: Any,
        db: Any,
        *,
        pinned: str | None = None,
        max_failures: int = 5,
        clock: Any = time.time,
    ) -> None:
        self.gemini = gemini
        self.keys = keypool
        self.db = db
        self.pinned = short(pinned) if pinned else None
        self.max_failures = max_failures
        self.clock = clock
        self.needs_discovery = True

    # ---- discovery and testing --------------------------------------------------------------
    async def discover(self) -> list[str]:
        key = self.keys.any_live()
        if key is None:
            log.warning("model discovery skipped: no live Gemini key")
            return []
        try:
            models = await self.gemini.list_models(key.key)
        except InvalidKey as exc:
            self.keys.mark_dead(key.id, str(exc))
            return []
        except GeminiError as exc:
            log.warning("model discovery failed: %s", exc)
            return []
        names = rank([m["name"] for m in models if is_candidate(m)])
        known = {r["name"]: r for r in self.db.model_rows()}
        for i, n in enumerate(names):
            if n in known:
                self.db.upsert_model(n, rank=i)
            else:
                self.db.upsert_model(n, rank=i, status="untested")
        for n, row in known.items():
            if n not in names and row["status"] != "gone":
                self.db.upsert_model(n, status="gone", rank=999)
        self.needs_discovery = False
        log.info("model discovery: %d candidate(s)", len(names))
        return names

    async def test(self, name: str) -> bool:
        body = {
            "contents": [{"role": "user", "parts": [{"text": "Call the ping tool now."}]}],
            "tools": [PING_TOOL],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": {"temperature": 0, "maxOutputTokens": 64},
        }
        for _ in range(max(1, len(self.keys.keys))):
            key = self.keys.acquire(name)
            if key is None:
                return False
            try:
                resp = await self.gemini.generate(key.key, name, body)
            except RateLimited as exc:
                self.keys.rate_limited(key.id, name, exc.retry_after, exc.per_day)
                continue
            except InvalidKey as exc:
                self.keys.mark_dead(key.id, str(exc))
                continue
            except Forbidden:
                self.forbidden(name, key.id)
                continue
            except ModelNotFound:
                self.gone(name)
                return False
            except GeminiError as exc:
                self.failure(name, str(exc))
                return False
            self.keys.success(key.id, name)
            calls = function_calls(resp)
            if any(c.get("name") == "ping" for c in calls):
                self.success(name)
                return True
            self.failure(name, "no function call in test response")
            return False
        return False

    async def daily(self) -> str | None:
        """Re-discover, revive dead models, then make sure the best candidate works."""
        await self.discover()
        for row in self.db.model_rows():
            if row["status"] in ("dead", "notfree"):
                self.db.upsert_model(row["name"], status="untested", consecutive_failures=0)
        for name in self.candidates():
            if await self.test(name):
                return name
        return None

    # ---- selection --------------------------------------------------------------------------
    def candidates(self) -> list[str]:
        now = self.clock()
        rows = self.db.model_rows()
        usable = [
            r["name"]
            for r in rows
            if r["status"] in USABLE and float(r["cooldown_until"] or 0) <= now
        ]
        usable.sort(key=lambda n: (0 if self._status(n) == "ok" else 1, rank_key(n), n))
        if self.pinned:
            row = self.db.model_row(self.pinned)
            if row is None or row["status"] in USABLE:
                usable = [self.pinned] + [n for n in usable if n != self.pinned]
        return usable

    def current(self) -> str | None:
        c = self.candidates()
        return c[0] if c else None

    def _status(self, name: str) -> str | None:
        row = self.db.model_row(name)
        return row["status"] if row else None

    # ---- outcome tracking -------------------------------------------------------------------
    def _ensure(self, name: str) -> None:
        if self.db.model_row(name) is None:
            self.db.upsert_model(name, rank=999, status="untested")

    def success(self, name: str) -> None:
        self._ensure(name)
        self.db.upsert_model(
            name, status="ok", consecutive_failures=0, last_ok=self.clock(), cooldown_until=0
        )

    def failure(self, name: str, error: str) -> None:
        self._ensure(name)
        row = self.db.model_row(name)
        n = int(row["consecutive_failures"] or 0) + 1
        status = "dead" if n >= self.max_failures else row["status"]
        self.db.upsert_model(name, consecutive_failures=n, status=status, last_error=error[:200])
        if status == "dead":
            log.warning("model %s marked dead after %d failures", name, n)

    def rate_limited(self, name: str) -> None:
        """Called when *every* key is cooling down for this model."""
        self._ensure(name)
        until = self.keys.earliest_available(name) or (self.clock() + 60)
        self.db.upsert_model(name, cooldown_until=until, last_error="rate limited on all keys")
        self.failure(name, "rate limited on all keys")

    def forbidden(self, name: str, key_id: str) -> None:
        self.keys.cooldown(key_id, name, 24 * 3600)
        if self.keys.acquire_peek(name) is None:
            self._ensure(name)
            self.db.upsert_model(name, status="notfree", last_error="403 on every key")
            log.info("model %s looks not free (403 on every key)", name)

    def gone(self, name: str) -> None:
        self._ensure(name)
        self.db.upsert_model(name, status="gone", last_error="404 / deprecated")
        self.needs_discovery = True

    def status(self) -> list[dict[str, Any]]:
        now = self.clock()
        out = []
        for r in self.db.model_rows():
            cd = float(r["cooldown_until"] or 0)
            out.append(
                {
                    "name": r["name"],
                    "status": r["status"],
                    "failures": r["consecutive_failures"],
                    "cooldown_s": max(0, round(cd - now)),
                    "pinned": r["name"] == self.pinned,
                }
            )
        return out
