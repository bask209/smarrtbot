"""Pending actions: the only path from a proposal to a state change.

A proposal stores everything the action needs (ids, file lists, sizes) in a ``pending_actions``
row, built by code from service data. The inline button carries only the random nonce. A
confirmation must present the nonce before expiry, from the same Telegram user who started the
action, and it wins an atomic compare-and-set so the action runs exactly once.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from smarrtbot.db import Database, PendingAction

CONFIRM_PREFIX = "ok:"
CANCEL_PREFIX = "no:"


class ClaimStatus(StrEnum):
    OK = "ok"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    USED = "used"
    NOT_REQUESTER = "not_requester"


@dataclass
class Claim:
    status: ClaimStatus
    action: PendingAction | None = None


@dataclass
class Proposal:
    """What a flow hands back: the nonce plus code-built facts. The bot renders them per language;
    nothing here comes from model text."""

    nonce: str
    kind: str
    label: str
    size: int | None = None
    details: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def new_nonce() -> str:
    return secrets.token_urlsafe(12)  # 16 chars, fits callback_data (64 bytes) with a prefix


class PendingActions:
    def __init__(self, db: Database, ttl_seconds: float, clock: Any = time.time) -> None:
        self.db = db
        self.ttl = ttl_seconds
        self.clock = clock

    def create(
        self,
        kind: str,
        requester_id: int,
        chat_id: int,
        payload: dict[str, Any],
        ttl: float | None = None,
    ) -> str:
        nonce = new_nonce()
        self.db.insert_pending(nonce, kind, requester_id, chat_id, payload, ttl or self.ttl)
        return nonce

    def claim(self, nonce: str, user_id: int) -> Claim:
        action = self.db.get_pending(nonce)
        if action is None:
            return Claim(ClaimStatus.UNKNOWN)
        if action.status != "pending":
            return Claim(ClaimStatus.USED, action)
        if action.expires_at < self.clock():
            self.db.transition_pending(nonce, "pending", "expired")
            return Claim(ClaimStatus.EXPIRED, action)
        if action.requester_id != user_id:
            return Claim(ClaimStatus.NOT_REQUESTER, action)
        if not self.db.transition_pending(nonce, "pending", "running"):
            return Claim(ClaimStatus.USED, action)
        return Claim(ClaimStatus.OK, action)

    def cancel(self, nonce: str, user_id: int) -> Claim:
        action = self.db.get_pending(nonce)
        if action is None:
            return Claim(ClaimStatus.UNKNOWN)
        if action.requester_id != user_id:
            return Claim(ClaimStatus.NOT_REQUESTER, action)
        if not self.db.transition_pending(nonce, "pending", "cancelled"):
            return Claim(ClaimStatus.USED, action)
        return Claim(ClaimStatus.OK, action)

    def finish(self, nonce: str, ok: bool, result: str) -> None:
        self.db.finish_pending(nonce, "done" if ok else "failed", result[:2000])

    def purge(self) -> None:
        self.db.purge_pending(self.clock() - 7 * 86400)
