"""``smarrtbot setup-notify``: Radarr/Sonarr built-in Telegram connection for download notices.

Creates or updates a notification named ``smarrtbot`` (implementation ``Telegram``) in each app,
using the bot token from the mounted secret and the chat id given on the command line. This stores
the token inside Radarr/Sonarr (their own config), which is how their Telegram connection works.
Dry run by default; ``--apply`` writes and then asks the app to send a test message.
"""

from __future__ import annotations

from typing import Any

NAME = "smarrtbot"
EVENTS = {
    "onGrab": True,
    "onDownload": True,
    "onUpgrade": True,
    "onHealthIssue": True,
    "includeHealthWarnings": False,
    "onHealthRestored": True,
    "onManualInteractionRequired": True,
}


def _set_field(fields: list[dict[str, Any]], name: str, value: Any) -> None:
    for f in fields:
        if f.get("name") == name:
            f["value"] = value
            return
    fields.append({"name": name, "value": value})


def _field(fields: list[dict[str, Any]], name: str) -> Any:
    return next((f.get("value") for f in fields if f.get("name") == name), None)


async def plan_and_apply(
    clients: dict[str, Any], token: str, chat_id: int, apply: bool = False, test: bool = True
) -> list[str]:
    out: list[str] = []
    for app, client in clients.items():
        existing = next((n for n in await client.notifications() if n.get("name") == NAME), None)
        if existing is None:
            schema = next(
                (
                    s
                    for s in await client.notification_schema()
                    if s.get("implementation") == "Telegram"
                ),
                None,
            )
            if schema is None:
                out.append(f"[{app}] Telegram notification type not available")
                continue
            body = dict(schema)
            action = "create"
        else:
            body = dict(existing)
            action = "update"
        fields = [dict(f) for f in body.get("fields", [])]
        same = (
            existing is not None
            and _field(fields, "botToken") == token
            and str(_field(fields, "chatId")) == str(chat_id)
            and all(existing.get(k) == v for k, v in EVENTS.items() if k in existing)
        )
        if same:
            out.append(f"[{app}] notification '{NAME}': unchanged")
            continue
        _set_field(fields, "botToken", token)
        _set_field(fields, "chatId", str(chat_id))
        _set_field(fields, "sendSilently", False)
        body.update({"name": NAME, "fields": fields, **EVENTS})
        for k in list(EVENTS):
            if f"supports{k[2:]}" in body and not body.get(f"supports{k[2:]}"):
                body[k] = False
        out.append(f"[{app}] notification '{NAME}': {action} (chat {chat_id}, token <redacted>)")
        if apply:
            if action == "create":
                await client.create_notification(body)
            else:
                await client.update_notification(body)
            if test:
                await client.test_notification(body)
                out.append(f"[{app}] test message sent")
    return out
