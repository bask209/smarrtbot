"""Small shared helpers."""

from __future__ import annotations

import html
import re
import time
import unicodedata

GB = 1024**3
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")


def human_bytes(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{sign}{n:.0f} {unit}" if unit in ("B", "KB") else f"{sign}{n:.1f} {unit}"
        n /= 1024
    return f"{sign}{n:.1f} TB"  # pragma: no cover


def untrusted(text: object, limit: int = 300) -> str:
    """Normalize text that comes from an external service before it reaches the model.

    It stays *data*: we strip control and bidi characters and truncate. It is only ever placed
    inside JSON tool results, never in instructions.
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = _CONTROL.sub(" ", s)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def now() -> float:
    return time.time()


def h(text: object) -> str:
    """Escape for Telegram HTML parse mode."""
    return html.escape(str(text), quote=False)
