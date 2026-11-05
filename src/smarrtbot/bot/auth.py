"""The single gate in front of every handler.

A request passes only if the chat is a private chat or an allowlisted group, *and* the user is
the owner or approved. Anything else gets no Gemini call; only /whoami works.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str = ""


def chat_allowed(settings: Any, chat_id: int, chat_type: str) -> bool:
    if chat_type == "private":
        return True
    return chat_type in ("group", "supergroup") and chat_id in settings.telegram.allowed_group_ids


def is_owner(settings: Any, user_id: int | None) -> bool:
    return bool(user_id) and user_id == settings.telegram.owner_id


def user_approved(settings: Any, db: Any, user_id: int | None) -> bool:
    if not user_id:
        return False
    return is_owner(settings, user_id) or db.user_status(user_id) == "approved"


def gate(settings: Any, db: Any, chat_id: int, chat_type: str, user_id: int | None) -> GateResult:
    if not settings.telegram.owner_id:
        return GateResult(False, "owner not configured")
    if not chat_allowed(settings, chat_id, chat_type):
        return GateResult(False, "chat not allowed")
    if not user_approved(settings, db, user_id):
        return GateResult(False, "user not approved")
    return GateResult(True)
