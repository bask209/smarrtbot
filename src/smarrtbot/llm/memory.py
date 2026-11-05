"""Per-chat memory: recent turns plus a rolling summary, in SQLite."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

Summarizer = Callable[[str, list[tuple[str, str]]], Awaitable[str | None]]


class ChatMemory:
    def __init__(self, db: Any, keep_turns: int = 12, summarize_after: int = 24) -> None:
        self.db = db
        self.keep = keep_turns
        self.after = max(summarize_after, keep_turns + 2)

    def context(self, chat_id: int) -> tuple[str, list[tuple[str, str]]]:
        rows = self.db.turns(chat_id, self.keep)
        return self.db.summary(chat_id), [(r["role"], r["text"]) for r in rows]

    def add(self, chat_id: int, role: str, text: str) -> None:
        self.db.add_turn(chat_id, role, text[:4000])

    async def maybe_summarize(self, chat_id: int, summarizer: Summarizer) -> None:
        if self.db.count_turns(chat_id) <= self.after:
            return
        old = self.db.oldest_turns(chat_id, self.keep)
        if not old:
            return
        previous = self.db.summary(chat_id)
        turns = [(r["role"], r["text"]) for r in old]
        new = await summarizer(previous, turns)
        if not new:  # AI busy: fall back to a truncated plain-text digest
            digest = " | ".join(f"{role}: {text[:80]}" for role, text in turns[-6:])
            new = (previous + " | " + digest).strip(" |")[-1500:]
        self.db.set_summary(chat_id, new[:2000])
        self.db.delete_turns_upto(chat_id, int(old[-1]["id"]))

    def clear(self, chat_id: int) -> None:
        self.db.clear_memory(chat_id)
