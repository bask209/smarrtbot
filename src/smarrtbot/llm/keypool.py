"""Pool of Gemini API keys from a hot-reloaded file.

* one key per line (``#`` comments and blank lines ignored); edit the file to add or remove keys,
  never via chat;
* per-(key, model) cooldowns set from the 429 retry hint; per-day quota exhaustion cools down
  until the next Pacific midnight (when Google resets free quotas);
* invalid keys are marked dead (until the file changes);
* usage counters per key are written to SQLite under a short hash id; the key itself is
  registered with the log redactor and never stored or logged.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from smarrtbot.logsafe import register_secret, secret_id

log = logging.getLogger(__name__)
_PACIFIC = dt.timezone(dt.timedelta(hours=-8))  # PST; at most an hour early during PDT


@dataclass
class KeyState:
    id: str
    key: str
    dead: bool = False
    dead_reason: str = ""


def seconds_until_pacific_midnight(now: float) -> float:
    t = dt.datetime.fromtimestamp(now, _PACIFIC)
    nxt = (t + dt.timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return max(60.0, (nxt - t).total_seconds())


class KeyPool:
    def __init__(self, path: Path | str, db: Any = None, clock: Any = time.time) -> None:
        self.path = Path(path)
        self.db = db
        self.clock = clock
        self._mtime: float | None = None
        self._keys: list[KeyState] = []
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._last_used: dict[str, float] = {}

    def reload(self) -> None:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            if self._keys:
                log.warning("gemini keys file is gone; keeping %d loaded key(s)", len(self._keys))
            return
        if mtime == self._mtime:
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warning("cannot read gemini keys file: %s", exc.strerror)
            return
        keys: list[KeyState] = []
        for raw in lines:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            kid = secret_id(line)
            if any(k.id == kid for k in keys):
                continue
            register_secret(line, f"<gemini-key:{kid}>")
            # Any edit of the file revives dead keys (the operator may have fixed them).
            keys.append(KeyState(kid, line))
        self._keys = keys
        self._mtime = mtime
        log.info("gemini keys loaded: %d", len(keys))

    @property
    def keys(self) -> list[KeyState]:
        self.reload()
        return list(self._keys)

    def acquire(self, model: str) -> KeyState | None:
        """Least-recently-used live key that is not cooling down for this model."""
        now = self.clock()
        live = [
            k for k in self.keys if not k.dead and self._cooldowns.get((k.id, model), 0.0) <= now
        ]
        if not live:
            return None
        k = min(live, key=lambda x: self._last_used.get(x.id, 0.0))
        self._last_used[k.id] = now
        return k

    def any_live(self) -> KeyState | None:
        live = [k for k in self.keys if not k.dead]
        return live[0] if live else None

    def cooldown(self, key_id: str, model: str, seconds: float) -> None:
        self._cooldowns[(key_id, model)] = self.clock() + max(1.0, seconds)

    def rate_limited(self, key_id: str, model: str, retry_after: float, per_day: bool) -> None:
        wait = seconds_until_pacific_midnight(self.clock()) if per_day else retry_after
        self.cooldown(key_id, model, wait)
        self._record(key_id, model, "rate_limited")

    def mark_dead(self, key_id: str, reason: str) -> None:
        for k in self._keys:
            if k.id == key_id:
                k.dead, k.dead_reason = True, reason[:120]
                log.warning("gemini key %s marked dead: %s", key_id, k.dead_reason)
        self._record(key_id, "-", "error")

    def success(self, key_id: str, model: str) -> None:
        self._record(key_id, model, "ok")

    def error(self, key_id: str, model: str) -> None:
        self._record(key_id, model, "error")

    def earliest_available(self, model: str) -> float | None:
        now = self.clock()
        times = [max(now, self._cooldowns.get((k.id, model), 0.0)) for k in self.keys if not k.dead]
        return min(times) if times else None

    def acquire_peek(self, model: str) -> KeyState | None:
        now = self.clock()
        for k in self.keys:
            if not k.dead and self._cooldowns.get((k.id, model), 0.0) <= now:
                return k
        return None

    def _record(self, key_id: str, model: str, outcome: str) -> None:
        if self.db is not None:
            day = dt.datetime.fromtimestamp(self.clock(), dt.UTC).date().isoformat()
            self.db.record_key_usage(key_id, model, day, outcome)

    def status(self) -> list[dict[str, Any]]:
        now = self.clock()
        out = []
        for k in self.keys:
            cooling = {
                m: round(t - now)
                for (kid, m), t in self._cooldowns.items()
                if kid == k.id and t > now
            }
            out.append({"id": k.id, "dead": k.dead, "reason": k.dead_reason, "cooling": cooling})
        return out
