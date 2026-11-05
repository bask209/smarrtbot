"""Logging with secret redaction.

Every secret the process loads is registered here; a logging filter replaces it in all records.
httpx logs full request URLs at INFO, and Telegram's Bot API puts the bot token in the URL, so
noisy HTTP loggers are also capped at WARNING.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading

_lock = threading.Lock()
_secrets: dict[str, str] = {}
# No leading \b: the Bot API URL form is ".../bot<TOKEN>/method".
_TG_TOKEN_RE = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")
_GOOGLE_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")


def secret_id(value: str) -> str:
    """Short stable label for a secret, safe to log and store."""
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def register_secret(value: str | None, label: str | None = None) -> None:
    if not value or len(value) < 6:
        return
    with _lock:
        _secrets[value] = label or f"<redacted:{secret_id(value)}>"


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        items = sorted(_secrets.items(), key=lambda kv: -len(kv[0]))
    for value, label in items:
        if value in text:
            text = text.replace(value, label)
    text = _TG_TOKEN_RE.sub("<redacted:telegram-token>", text)
    return _GOOGLE_KEY_RE.sub("<redacted:google-key>", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            return True
        clean = redact(msg)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if clean != msg or record.args:
            record.msg, record.args = clean, None
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
