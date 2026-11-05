"""Handler-level checks with lightweight stand-ins for Telegram objects."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from smarrtbot.bot.handlers import BotHandlers
from smarrtbot.flows import delete as d
from smarrtbot.flows.actions import CONFIRM_PREFIX
from tests.fakes import ALICE, BOB, GROUP, OWNER, add_movie, make_services


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    async def nosleep(_s):
        return None

    monkeypatch.setattr(d.asyncio, "sleep", nosleep)


def user(uid, name="U"):
    return SimpleNamespace(
        id=uid, first_name=name, username=None, language_code="en", full_name=name
    )


def message(text=""):
    return SimpleNamespace(
        text=text, reply_html=AsyncMock(), reply_text=AsyncMock(), reply_to_message=None
    )


def update(uid, chat_id=GROUP, chat_type="supergroup", text="", data=None):
    chat = SimpleNamespace(id=chat_id, type=chat_type, title="G")
    msg = message(text)
    q = None
    if data is not None:
        q = SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(chat=chat),
        )
    return SimpleNamespace(
        effective_user=user(uid), effective_chat=chat, effective_message=msg, callback_query=q
    )


def context(args=None):
    bot = SimpleNamespace(
        id=999, username="smarrtbot", send_message=AsyncMock(), send_chat_action=AsyncMock()
    )
    return SimpleNamespace(args=args or [], bot=bot)


async def test_unapproved_user_gets_nothing_and_no_ai(tmp_path):
    svc, _ = make_services(tmp_path)
    svc.agent = SimpleNamespace(respond=AsyncMock())
    hd = BotHandlers(svc)
    up = update(4444, chat_id=4444, chat_type="private", text="add a movie")
    await hd.text(up, context())
    svc.agent.respond.assert_not_called()
    assert "/whoami" in up.effective_message.reply_text.call_args.args[0]
    # In a group that is not allowlisted, the bot stays silent.
    up2 = update(ALICE, chat_id=-777, text="@smarrtbot hi")
    await hd.text(up2, context())
    svc.agent.respond.assert_not_called()
    up2.effective_message.reply_text.assert_not_called()


async def test_group_needs_mention_or_reply(tmp_path):
    svc, _ = make_services(tmp_path)
    svc.agent = SimpleNamespace(
        respond=AsyncMock(return_value=SimpleNamespace(text="ok", proposals=[]))
    )
    hd = BotHandlers(svc)
    await hd.text(update(ALICE, text="just chatting"), context())
    svc.agent.respond.assert_not_called()
    await hd.text(update(ALICE, text="@smarrtbot how much space?"), context())
    args = svc.agent.respond.call_args.args
    assert args[2] == "how much space?" and args[3] == "en"


async def test_whoami_requests_owner_approval_and_owner_taps(tmp_path):
    svc, _ = make_services(tmp_path)
    hd = BotHandlers(svc)
    ctx = context()
    await hd.whoami(update(5555, chat_id=5555, chat_type="private"), ctx)
    send = ctx.bot.send_message.call_args
    assert send.args[0] == OWNER
    approve_nonce = send.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    # A non-owner cannot approve.
    up = update(ALICE, chat_id=ALICE, chat_type="private", data=approve_nonce)
    await hd.callback(up, ctx)
    assert svc.db.user_status(5555) == "pending"
    up = update(OWNER, chat_id=OWNER, chat_type="private", data=approve_nonce)
    await hd.callback(up, ctx)
    assert svc.db.user_status(5555) == "approved"
    # Asking again within a day does not spam the owner.
    ctx2 = context()
    await hd.whoami(update(6666, chat_id=6666, chat_type="private"), ctx2)
    await hd.whoami(update(6666, chat_id=6666, chat_type="private"), ctx2)
    assert ctx2.bot.send_message.call_count == 1


async def test_delete_command_confirm_only_by_requester(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11")
    hd = BotHandlers(svc)
    up = update(ALICE)
    await hd.delete(up, context(["alpha"]))
    markup = up.effective_message.reply_html.call_args.kwargs["reply_markup"]
    btn = markup.inline_keyboard[0][0]
    assert "no undo" in btn.text and "MB" in btn.text
    data = btn.callback_data
    assert data.startswith(CONFIRM_PREFIX) and len(data) <= 64
    bob_tap = update(BOB, data=data)
    await hd.callback(bob_tap, context())
    assert bob_tap.callback_query.answer.call_args.kwargs.get("show_alert")
    assert svc.radarr.calls == []
    alice_tap = update(ALICE, data=data)
    await hd.callback(alice_tap, context())
    assert svc.radarr.calls == [("delete_movie", 1)]
    text = alice_tap.callback_query.edit_message_text.call_args.args[0]
    assert "Deleted" in text and "seeding" in text
    # Replaying the same button does nothing.
    await hd.callback(update(ALICE, data=data), context())
    assert svc.radarr.calls == [("delete_movie", 1)]


async def test_revoke_is_owner_only(tmp_path):
    svc, _ = make_services(tmp_path)
    hd = BotHandlers(svc)
    await hd.revoke(update(ALICE), context([str(BOB)]))
    assert svc.db.user_status(BOB) == "approved"
    await hd.revoke(update(OWNER), context([str(BOB)]))
    assert svc.db.user_status(BOB) == "revoked"


async def test_storage_command_renders(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11")
    hd = BotHandlers(svc)
    up = update(ALICE, text="")
    up.effective_user.language_code = "es"
    await hd.storage(up, context())
    out = up.effective_message.reply_html.call_args.args[0]
    assert "Disco" in out and "Alpha" in out
