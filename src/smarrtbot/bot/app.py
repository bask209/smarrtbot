"""Application wiring: handlers, scheduled jobs, health server, long polling."""

from __future__ import annotations

import datetime as dt
import json
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from smarrtbot.bot.handlers import TV_PREFIX, BotHandlers, tv_label
from smarrtbot.bot.i18n import t
from smarrtbot.config import read_secret
from smarrtbot.flows import storage as storage_flow
from smarrtbot.flows import tvcheck
from smarrtbot.health import HealthServer, HealthState
from smarrtbot.logsafe import register_secret
from smarrtbot.services import Services
from smarrtbot.util import h, human_bytes

log = logging.getLogger(__name__)


class Jobs:
    def __init__(self, svc: Services, handlers: BotHandlers, health: HealthState) -> None:
        self.svc = svc
        self.handlers = handlers
        self.health = health

    def _lang(self) -> str:
        row = self.svc.db.user(self.svc.settings.telegram.owner_id)
        return (row["lang"] if row else None) or "en"

    def _targets(self) -> list[int]:
        tg = self.svc.settings.telegram
        out = [tg.owner_id] if tg.owner_id else []
        target = tg.notify_target()
        if target is not None and target not in out:
            out.append(target)
        return out

    async def heartbeat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.health.heartbeat = time.time()

    async def tv_check(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.svc.settings.tvcheck.enabled:
            return
        try:
            await tvcheck.poll_imports(self.svc)
            flagged = await tvcheck.inspect_due(self.svc)
        except Exception:
            log.exception("tv check failed")
            return
        target = self.svc.settings.telegram.notify_target()
        if target is None:
            return
        lang = self._lang()
        for fid in flagged:
            row = self.svc.db.flag(fid)
            if row is None:
                continue
            info = json.loads(row["issues"] or "{}")
            lines = []
            for code in info.get("issues", []):
                mbps = round((info.get("bitrate_kbps") or 0) / 1000, 1)
                lines.append(t(lang, f"issue.{code}", mbps=mbps))
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(t(lang, "tv_btn"), callback_data=f"{TV_PREFIX}{fid}")]]
            )
            try:
                await context.bot.send_message(
                    target,
                    t(lang, "tv_flag", label=h(tv_label(row)), issues="\n".join(lines)),
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
            except TelegramError as exc:
                log.warning("could not post TV flag: %s", exc)

    async def storage_alert(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            rep = await storage_flow.check_alert(self.svc)
        except Exception:
            log.exception("storage alert check failed")
            return
        if rep is None:
            return
        lang = self._lang()
        pct = f"{rep.free_percent:.1f}%" if rep.free_percent is not None else "?"
        for chat in self._targets():
            try:
                await context.bot.send_message(
                    chat, t(lang, "alert", free=human_bytes(rep.free), pct=pct)
                )
            except TelegramError as exc:
                log.warning("could not send storage alert: %s", exc)

    async def snapshot(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            rep = await storage_flow.take_snapshot(self.svc)
        except Exception:
            log.exception("storage snapshot failed")
            return
        s = self.svc.settings.storage
        if s.weekly_summary and dt.date.today().weekday() == 0:
            lang = self._lang()
            text = t(lang, "weekly") + self.handlers.format_storage(lang, rep)
            for chat in self._targets():
                try:
                    await context.bot.send_message(chat, text, parse_mode=ParseMode.HTML)
                except TelegramError as exc:
                    log.warning("could not send weekly summary: %s", exc)

    async def scan(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.svc.scanner.refresh()
        except Exception:
            log.exception("scan failed")

    async def models(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.svc.agent is None:
            return
        try:
            best = await self.svc.agent.models.daily()
            log.info("model manager: current best %s", best)
        except Exception:
            log.exception("model manager job failed")

    async def purge(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.svc.pending.purge()


def build_application(svc: Services) -> Application:
    settings = svc.settings
    token = read_secret(settings.telegram.token_file, name="telegram token")
    register_secret(token, "<telegram-token>")
    health = HealthState()
    handlers = BotHandlers(svc)
    jobs = Jobs(svc, handlers, health)
    server = HealthServer(health, settings.health_host, settings.health_port)

    async def post_init(app: Application) -> None:
        await server.start()
        health.polling = True
        jq = app.job_queue
        assert jq is not None  # noqa: S101 - installed via the [job-queue] extra
        jq.run_repeating(jobs.heartbeat, interval=30, first=1)
        jq.run_repeating(jobs.tv_check, interval=settings.tvcheck.interval_minutes * 60, first=90)
        jq.run_repeating(
            jobs.storage_alert, interval=settings.storage.alert_check_minutes * 60, first=120
        )
        jq.run_daily(jobs.snapshot, time=dt.time(3, 30))
        jq.run_once(jobs.snapshot, when=300)
        jq.run_repeating(jobs.scan, interval=settings.storage.scan_interval_hours * 3600, first=600)
        jq.run_repeating(jobs.models, interval=86400, first=20)
        jq.run_daily(jobs.purge, time=dt.time(4, 0))
        me = await app.bot.get_me()
        log.info(
            "smarrtbot started as @%s (dry_run=%s, groups=%d)",
            me.username,
            settings.dry_run,
            len(settings.telegram.allowed_group_ids),
        )

    async def post_shutdown(app: Application) -> None:
        health.polling = False
        await server.stop()
        await svc.aclose()

    app = (
        Application.builder()
        .token(token)
        .concurrent_updates(8)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    hd = handlers
    for name, fn in (
        ("start", hd.help),
        ("help", hd.help),
        ("whoami", hd.whoami),
        ("status", hd.status),
        ("queue", hd.queue),
        ("storage", hd.storage),
        ("cleanup", hd.cleanup),
        ("delete", hd.delete),
        ("models", hd.models),
        ("forget", hd.forget),
        ("revoke", hd.revoke),
        ("users", hd.users),
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(hd.callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, hd.text))
    app.add_error_handler(_on_error)
    return app


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler error: %s", type(context.error).__name__, exc_info=context.error)


def run(svc: Services) -> None:
    app = build_application(svc)
    app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])


__all__ = ["Jobs", "build_application", "run"]
