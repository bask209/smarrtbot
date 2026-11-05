"""Telegram handlers. Every entry point goes through ``_gate`` first."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from smarrtbot.bot import auth
from smarrtbot.bot.i18n import detect_lang, render_details, t
from smarrtbot.clients.http import ServiceError
from smarrtbot.flows import add as add_flow
from smarrtbot.flows import cleanup as cleanup_flow
from smarrtbot.flows import delete as delete_flow
from smarrtbot.flows import tvcheck
from smarrtbot.flows.actions import CANCEL_PREFIX, CONFIRM_PREFIX, ClaimStatus, Proposal
from smarrtbot.flows.library import library, match_titles
from smarrtbot.flows.storage import StorageReport, storage_report
from smarrtbot.llm.agent import AIUnavailable
from smarrtbot.llm.tools import queue_items
from smarrtbot.util import h, human_bytes

log = logging.getLogger(__name__)

TV_PREFIX = "tv:"
ACCESS_REQUEST_COOLDOWN = 24 * 3600
SEASON_RE = re.compile(r"(?i)\s+(?:s|season\s*|temporada\s*|temp\.?\s*)(\d{1,3})\s*$")


class BotHandlers:
    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self.settings = svc.settings

    # ---- helpers ----------------------------------------------------------------------------
    def _lang(self, user: User | None, text: str = "") -> str:
        stored = None
        if user is not None:
            row = self.svc.db.user(user.id)
            stored = row["lang"] if row else None
        fallback = stored or (
            "es" if user is not None and (user.language_code or "").startswith("es") else "en"
        )
        lang = detect_lang(text, fallback) if text else fallback
        if user is not None and text and lang != stored and self.svc.db.user(user.id):
            self.svc.db.set_user_lang(user.id, lang)
        return lang

    def _gate(self, update: Update) -> auth.GateResult:
        chat, user = update.effective_chat, update.effective_user
        if chat is None or user is None:
            return auth.GateResult(False, "no chat/user")
        self.svc.db.touch_chat(chat.id, chat.type, getattr(chat, "title", None))
        return auth.gate(self.settings, self.svc.db, chat.id, chat.type, user.id)

    async def _deny(self, update: Update, lang: str) -> None:
        chat = update.effective_chat
        # Stay silent in groups that are not allowlisted; hint in DMs.
        if chat is not None and chat.type == "private" and update.effective_message:
            await update.effective_message.reply_text(t(lang, "not_allowed"))

    async def _reply(self, update: Update, text: str, markup: Any = None) -> Message | None:
        msg = update.effective_message
        if msg is None:
            return None
        return await msg.reply_html(text, reply_markup=markup, disable_web_page_preview=True)

    def _dry(self, lang: str) -> str:
        return t(lang, "dry") if self.svc.dry_run else ""

    def render_proposal(
        self, lang: str, p: Proposal, requester: str
    ) -> tuple[str, InlineKeyboardMarkup]:
        size = human_bytes(p.size) if p.size is not None else ""
        if p.kind == "add":
            kind = t(lang, f"kind.{p.details[0] if p.details else 'movie'}")
            text = t(lang, "add_text", kind=kind, label=h(p.label))
            button = t(lang, "add_btn", label=p.label)
        elif p.kind == "delete":
            text = t(lang, "del_text", label=h(p.label), details=render_details(lang, p.details))
            button = t(lang, "del_btn", size=size)
        elif p.kind == "cleanup":
            text = t(
                lang,
                "clean_text",
                label=h(p.label),
                size=size,
                details=render_details(lang, p.details),
            )
            button = t(lang, "clean_btn", size=size)
        elif p.kind == "tv_replace":
            text = t(lang, "tv_confirm", label=h(p.label))
            button = t(lang, "tv_confirm_btn")
        else:
            text, button = h(p.label), "OK"
        mins = max(1, int(self.settings.limits.pending_ttl_seconds // 60))
        text += "\n\n" + t(lang, "confirm_hint", name=h(requester), mins=mins)
        if self.svc.dry_run:
            text += "\n" + t(lang, "dry")
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(button[:64], callback_data=CONFIRM_PREFIX + p.nonce)],
                [
                    InlineKeyboardButton(
                        t(lang, "cancel_btn"), callback_data=CANCEL_PREFIX + p.nonce
                    )
                ],
            ]
        )
        return text, markup

    async def _send_proposal(self, update: Update, lang: str, p: Proposal) -> None:
        user = update.effective_user
        name = (user.first_name or user.username or str(user.id)) if user else "?"
        text, markup = self.render_proposal(lang, p, name)
        await self._reply(update, text, markup)

    # ---- commands ---------------------------------------------------------------------------
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lang = self._lang(update.effective_user)
        if not self._gate(update).allowed:
            return await self._deny(update, lang)
        await self._reply(update, t(lang, "help", dry=self._dry(lang)))

    async def whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat, user = update.effective_chat, update.effective_user
        if chat is None or user is None:
            return
        db = self.svc.db
        db.upsert_user(user.id, user.username, user.first_name)
        lang = self._lang(user)
        lines = [t(lang, "whoami", uid=user.id, cid=chat.id)]
        if auth.user_approved(self.settings, db, user.id):
            lines.append(t(lang, "whoami_approved"))
        elif (
            auth.chat_allowed(self.settings, chat.id, chat.type) and self.settings.telegram.owner_id
        ):
            key = f"access_req:{user.id}"
            last = float(db.kv_get(key, 0) or 0)
            if time.time() - last < ACCESS_REQUEST_COOLDOWN:
                lines.append(t(lang, "whoami_wait"))
            else:
                db.kv_set(key, time.time())
                await self._request_access(context, user, chat.title or "DM")
                lines.append(t(lang, "whoami_requested"))
        await self._reply(update, "\n".join(lines))

    async def _request_access(
        self, context: ContextTypes.DEFAULT_TYPE, user: User, where: str
    ) -> None:
        owner = self.settings.telegram.owner_id
        pending = self.svc.pending
        ttl = 7 * 86400
        payload = {"user_id": user.id}
        ok = pending.create("approve_user", owner, owner, payload, ttl=ttl)
        no = pending.create("deny_user", owner, owner, payload, ttl=ttl)
        lang = (
            self._lang(None)
            if not self.svc.db.user(owner)
            else (self.svc.db.user(owner)["lang"] or "en")
        )
        name = h(user.full_name or user.username or str(user.id))
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(t(lang, "approve_btn"), callback_data=CONFIRM_PREFIX + ok),
                    InlineKeyboardButton(t(lang, "deny_btn"), callback_data=CONFIRM_PREFIX + no),
                ]
            ]
        )
        try:
            await context.bot.send_message(
                owner,
                t(lang, "approve_req", name=name, uid=user.id, chat=h(where)),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except TelegramError as exc:
            log.warning("could not message the owner (start a DM with the bot first): %s", exc)

    async def revoke(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        lang = self._lang(user)
        if not self._gate(update).allowed:
            return await self._deny(update, lang)
        if not auth.is_owner(self.settings, user.id if user else None):
            await self._reply(update, t(lang, "owner_only"))
            return
        args = context.args or []
        if not args or not args[0].lstrip("-").isdigit():
            await self._reply(update, "/revoke &lt;user_id&gt;")
            return
        uid = int(args[0])
        self.svc.db.set_user_status(uid, "revoked", user.id if user else None)
        await self._reply(update, t(lang, "revoked", uid=uid))

    async def users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        lang = self._lang(user)
        if not self._gate(update).allowed:
            return await self._deny(update, lang)
        if not auth.is_owner(self.settings, user.id if user else None):
            await self._reply(update, t(lang, "owner_only"))
            return
        rows = self.svc.db.users()
        lines = [
            f"<code>{r['user_id']}</code> {h(r['first_name'] or r['username'] or '')}"
            f" – {r['status']}"
            for r in rows
        ]
        await self._reply(update, "\n".join(lines) or "-")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lang = self._lang(update.effective_user)
        if not self._gate(update).allowed:
            return await self._deny(update, lang)
        svc = self.svc

        async def arr(app: Any) -> str:
            if app is None:
                return "–"
            try:
                return "✅ " + str((await app.system_status()).get("version", ""))
            except ServiceError as exc:
                return f"❌ {h(exc)}"

        try:
            qbit = "✅ " + (await svc.qbit.version()) if svc.qbit else "–"
        except ServiceError as exc:
            qbit = f"❌ {h(exc)}"
        ms = ("✅ " if await svc.mediaserver.ping() else "❌ ") + svc.mediaserver.name
        model, keys = "–", "0"
        if svc.agent is not None:
            model = svc.agent.models.current() or "–"
            keys = str(len([k for k in svc.agent.keys.keys if not k.dead]))
        await self._reply(
            update,
            t(
                lang,
                "status",
                dry=self._dry(lang),
                radarr=await arr(svc.radarr),
                sonarr=await arr(svc.sonarr),
                qbit=qbit,
                ms=ms,
                model=h(model),
                keys=keys,
            ),
        )

    async def queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lang = self._lang(update.effective_user)
        if not self._gate(update).allowed:
            return await self._deny(update, lang)
        items = await queue_items(self.svc)
        if not items:
            await self._reply(update, t(lang, "queue_empty"))
            return
        lines = [
            f"• {h(i['title'])} – {i['progress']}% {h(i.get('eta') or '')}" for i in items[:25]
        ]
        await self._reply(update, "\n".join(lines))

    def format_storage(self, lang: str, rep: StorageReport) -> str:
        pct = f"{rep.free_percent:.1f}%" if rep.free_percent is not None else "?"
        tuniq = (
            t(lang, "storage_tuniq", u=human_bytes(rep.torrents_unique))
            if rep.torrents_unique is not None
            else ""
        )
        trend = ""
        if rep.delta_7d is not None or rep.delta_30d is not None:
            trend = t(
                lang, "storage_trend", d7=human_bytes(rep.delta_7d), d30=human_bytes(rep.delta_30d)
            )
        largest = "\n".join(f"• {h(x.label())} – {human_bytes(x.size)}" for x in rep.largest) or "–"
        return t(
            lang,
            "storage",
            used=human_bytes(rep.used),
            total=human_bytes(rep.total),
            free=human_bytes(rep.free),
            pct=pct,
            movies=human_bytes(rep.movies),
            tv=human_bytes(rep.tv),
            torrents=human_bytes(rep.torrents),
            tuniq=tuniq,
            trend=trend,
            largest=largest,
        )

    async def storage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lang = self._lang(update.effective_user)
        if not self._gate(update).allowed:
            return await self._deny(update, lang)
        rep = await storage_report(self.svc)
        await self._reply(update, self.format_storage(lang, rep))

    async def cleanup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lang = self._lang(update.effective_user)
        if not self._gate(update).allowed:
            return await self._deny(update, lang)
        cat = (context.args or [None])[0]
        if cat is not None and cat not in cleanup_flow.CATEGORIES:
            await self._reply(update, "/cleanup [" + "|".join(cleanup_flow.CATEGORIES) + "]")
            return
        items = await cleanup_flow.list_candidates(self.svc, cat)
        label = cat or "all"
        if not items:
            await self._reply(update, t(lang, "cleanup_none", cat=label))
            return
        lines = [
            f"<code>{c.id}</code> [{c.category}] {h(c.label[:70])} – {human_bytes(c.size)}"
            + (f" <i>({h(c.note)})</i>" if c.note else "")
            for c in items[:25]
        ]
        await self._reply(update, t(lang, "cleanup_list", cat=label, items="\n".join(lines)))

    async def delete(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        lang = self._lang(user)
        if not self._gate(update).allowed or user is None:
            return await self._deny(update, lang)
        query = " ".join(context.args or []).strip()
        if not query:
            await self._reply(update, t(lang, "usage_delete"))
            return
        season = None
        m = SEASON_RE.search(" " + query)
        if m:
            season = int(m.group(1))
            query = SEASON_RE.sub("", " " + query).strip()
        titles = await library(self.svc, "series" if season is not None else None)
        hits = match_titles(titles, query)
        if not hits:
            await self._reply(update, t(lang, "no_match", q=h(query)))
            return
        chat_id = update.effective_chat.id if update.effective_chat else user.id
        if len(hits) == 1:
            await self._propose_delete(
                update, lang, hits[0].kind, hits[0].id, season, user.id, chat_id
            )
            return
        rows = []
        for x in hits:
            kind = "season" if season is not None else x.kind
            nonce = self.svc.pending.create(
                "delete_pick", user.id, chat_id, {"kind": kind, "id": x.id, "season": season}
            )
            label = x.label() + (f" S{season:02d}" if season is not None else "")
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{label} – {human_bytes(x.size)}"[:64],
                        callback_data=CONFIRM_PREFIX + nonce,
                    )
                ]
            )
        await self._reply(update, t(lang, "pick_delete"), InlineKeyboardMarkup(rows))

    async def _propose_delete(
        self,
        update: Update,
        lang: str,
        kind: str,
        arr_id: int,
        season: int | None,
        user_id: int,
        chat_id: int,
    ) -> None:
        if kind == "series" and season is not None:
            kind = "season"
        try:
            p, _ = await delete_flow.propose_delete(
                self.svc,
                kind,
                arr_id,
                season,
                user_id,
                chat_id,  # type: ignore[arg-type]
            )
        except (delete_flow.DeleteError, ServiceError) as exc:
            await self._reply(update, t(lang, "failed", err=h(exc)))
            return
        await self._send_proposal(update, lang, p)

    async def models(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lang = self._lang(update.effective_user)
        if not self._gate(update).allowed:
            return await self._deny(update, lang)
        agent = self.svc.agent
        if agent is None:
            await self._reply(update, t(lang, "ai_busy"))
            return
        lines = [t(lang, "models_hdr")]
        for m in agent.models.status()[:12]:
            extra = f" ⏳{m['cooldown_s']}s" if m["cooldown_s"] else ""
            pin = " 📌" if m["pinned"] else ""
            lines.append(f"• {h(m['name'])} – {m['status']}{extra}{pin}")
        lines.append(t(lang, "keys_hdr"))
        usage = {
            r["key_id"]: r
            for r in self.svc.db.key_usage_today(time.strftime("%Y-%m-%d", time.gmtime()))
        }
        for k in agent.keys.status():
            u = usage.get(k["id"])
            counts = f" {u['requests']} req, {u['rate_limited']}×429" if u else ""
            state = "dead" if k["dead"] else ("cooling" if k["cooling"] else "ok")
            lines.append(f"• <code>{k['id']}</code> {state}{counts}")
        await self._reply(update, "\n".join(lines))

    async def forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lang = self._lang(update.effective_user)
        if not self._gate(update).allowed:
            return await self._deny(update, lang)
        if self.svc.agent is not None and update.effective_chat:
            self.svc.agent.memory.clear(update.effective_chat.id)
        await self._reply(update, t(lang, "forgot"))

    # ---- natural language -------------------------------------------------------------------
    def _addressed(self, update: Update, bot_id: int, bot_username: str) -> tuple[bool, str]:
        msg = update.effective_message
        chat = update.effective_chat
        if msg is None or not msg.text or chat is None:
            return False, ""
        text = msg.text
        if chat.type == "private":
            return True, text
        mention = f"@{bot_username}".lower() if bot_username else ""
        if mention and mention in text.lower():
            return True, re.sub(re.escape(mention), "", text, flags=re.IGNORECASE).strip()
        reply = msg.reply_to_message
        if reply is not None and reply.from_user is not None and reply.from_user.id == bot_id:
            return True, text
        return False, ""

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        addressed, text = self._addressed(update, context.bot.id, context.bot.username or "")
        if not addressed or not text:
            return
        user, chat = update.effective_user, update.effective_chat
        lang = self._lang(user, text)
        if not self._gate(update).allowed or user is None or chat is None:
            return await self._deny(update, lang)
        agent = self.svc.agent
        if agent is None:
            await self._reply(update, t(lang, "ai_busy"))
            return
        try:
            await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
        except TelegramError:
            pass
        try:
            reply = await agent.respond(chat.id, user.id, text[:2000], lang)
        except AIUnavailable:
            await self._reply(update, t(lang, "ai_busy"))
            return
        if reply.text:
            await self._reply(update, h(reply.text[:3500]))
        for p in reply.proposals[:5]:
            await self._send_proposal(update, lang, p)
        if not reply.text and not reply.proposals:
            await self._reply(update, t(lang, "no_reply"))

    # ---- callbacks --------------------------------------------------------------------------
    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        user = update.effective_user
        if q is None or user is None or not q.data:
            return
        lang = self._lang(user)
        data = q.data
        if data.startswith(TV_PREFIX):
            return await self._tv_tap(update, context, lang, data[len(TV_PREFIX) :])
        gate = self._gate(update)
        owner = auth.is_owner(self.settings, user.id)
        if not gate.allowed and not owner:
            await q.answer(t(lang, "not_allowed"), show_alert=True)
            return
        pending = self.svc.pending
        if data.startswith(CANCEL_PREFIX):
            claim = pending.cancel(data[len(CANCEL_PREFIX) :], user.id)
            if claim.status == ClaimStatus.NOT_REQUESTER:
                await q.answer(t(lang, "not_yours"), show_alert=True)
                return
            await q.answer()
            await self._edit(q, t(lang, "cancelled"))
            return
        if not data.startswith(CONFIRM_PREFIX):
            await q.answer()
            return
        claim = pending.claim(data[len(CONFIRM_PREFIX) :], user.id)
        if claim.status != ClaimStatus.OK:
            key = {
                ClaimStatus.NOT_REQUESTER: "not_yours",
                ClaimStatus.EXPIRED: "expired",
                ClaimStatus.USED: "used",
            }.get(claim.status, "unknown")
            await q.answer(t(lang, key), show_alert=True)
            return
        action = claim.action
        assert action is not None  # noqa: S101
        await q.answer(t(lang, "working"))
        try:
            text = await self._execute(
                update, context, lang, action.kind, action.payload, user.id, action.chat_id
            )
            pending.finish(action.nonce, True, text)
        except Exception as exc:
            log.warning("action %s failed: %s", action.kind, exc)
            text = t(lang, "failed", err=h(str(exc)[:300]))
            pending.finish(action.nonce, False, str(exc))
        if text:
            await self._edit(q, text)

    async def _edit(self, q: Any, text: str) -> None:
        try:
            await q.edit_message_text(
                text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except TelegramError as exc:
            log.debug("edit failed: %s", exc)

    async def _execute(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        lang: str,
        kind: str,
        payload: dict[str, Any],
        user_id: int,
        chat_id: int,
    ) -> str:
        svc, dry = self.svc, self.svc.dry_run
        if kind == "add":
            return h(await add_flow.execute_add(svc, payload, dry))
        if kind == "delete":
            r = await delete_flow.execute_delete(svc, payload, user_id, dry)
            extra = ""
            if r.torrents_seeding:
                extra += t(lang, "del_seeding")
            if r.torrents_kept:
                extra += t(lang, "del_kept", size=human_bytes(sum(s for _, s in r.torrents_kept)))
            if r.no_torrent:
                extra += t(lang, "del_none")
            if r.dry_run:
                return t(
                    lang,
                    "del_dry",
                    label=h(r.label),
                    expected=human_bytes(r.expected_freed),
                    extra=extra,
                )
            if r.mismatch:
                extra += t(
                    lang,
                    "del_mismatch",
                    freed=human_bytes(r.measured_freed),
                    expected=human_bytes(r.expected_freed),
                )
            for e in r.errors:
                extra += "\n⚠️ " + h(e)
            return t(
                lang,
                "del_done",
                prefix="",
                label=h(r.label),
                freed=human_bytes(r.measured_freed),
                expected=human_bytes(r.expected_freed),
                extra=extra,
            )
        if kind == "cleanup":
            c = await cleanup_flow.execute_cleanup(svc, payload, user_id, dry)
            skipped = ""
            if c.skipped:
                why = "; ".join(f"{h(label[:40])}: {h(reason)}" for label, reason in c.skipped[:5])
                skipped = t(lang, "clean_skipped", n=len(c.skipped), why=why)
            prefix = "[DRY RUN] " if c.dry_run else ""
            freed = human_bytes(sum(s for _, s in c.deleted)) if c.dry_run else human_bytes(c.freed)
            return t(
                lang, "clean_done", prefix=prefix, n=len(c.deleted), freed=freed, skipped=skipped
            )
        if kind == "tv_replace":
            return h(await tvcheck.execute_replace(svc, int(payload["flag_id"]), user_id, dry))
        if kind in ("approve_user", "deny_user"):
            uid = int(payload["user_id"])
            approve = kind == "approve_user"
            svc.db.set_user_status(uid, "approved" if approve else "denied", user_id)
            if approve:
                try:
                    ulang = (svc.db.user(uid) or {"lang": None})["lang"] or "en"
                    await context.bot.send_message(uid, t(ulang, "you_were_approved"))
                except TelegramError:
                    pass
            return t(lang, "approved_user" if approve else "denied_user", uid=uid)
        if kind == "delete_pick":
            await self._propose_delete(
                update,
                lang,
                payload["kind"],
                int(payload["id"]),
                payload.get("season"),
                user_id,
                chat_id,
            )
            return t(lang, "pick_delete") + " ✔"
        raise ValueError(f"unknown action {kind}")

    async def _tv_tap(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, lang: str, flag_id: str
    ) -> None:
        q = update.callback_query
        user = update.effective_user
        if q is None or user is None:
            return
        if not auth.user_approved(self.settings, self.svc.db, user.id) or not flag_id.isdigit():
            await q.answer(t(lang, "not_allowed"), show_alert=True)
            return
        row = self.svc.db.flag(int(flag_id))
        if row is None or row["status"] != "flagged":
            await q.answer(t(lang, "used"), show_alert=True)
            return
        await q.answer()
        chat_id = update.effective_chat.id if update.effective_chat else user.id
        nonce = self.svc.pending.create("tv_replace", user.id, chat_id, {"flag_id": int(flag_id)})
        p = Proposal(nonce=nonce, kind="tv_replace", label=tv_label(row))
        text, markup = self.render_proposal(lang, p, user.first_name or str(user.id))
        if q.message is not None:
            await context.bot.send_message(
                q.message.chat.id, text, parse_mode=ParseMode.HTML, reply_markup=markup
            )


def tv_label(row: Any) -> str:
    label = str(row["title"] or "?")
    if row["year"]:
        label += f" ({row['year']})"
    if row["season"] is not None and row["episode"] is not None:
        label += f" S{int(row['season']):02d}E{int(row['episode']):02d}"
    return label
