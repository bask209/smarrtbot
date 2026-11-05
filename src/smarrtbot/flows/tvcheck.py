"""TV-friendly check: poll *arr import history, inspect streams via the media server, flag.

No webhooks and no inbound port: a job polls ``/history/since`` with a cursor stored in SQLite.
The first run only sets the cursor to "now" (the existing library is not re-flagged). Each import
becomes a ``flags`` row that is inspected with exponential backoff, because the media server may
take a while to scan and analyse a new file.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any

from smarrtbot.clients.arr import GRAB_EVENT
from smarrtbot.clients.http import ServiceError
from smarrtbot.flows.delete import plan_delete
from smarrtbot.mediaserver.base import StreamReport

log = logging.getLogger(__name__)

ISSUES = ("dolby_vision", "truehd", "dts", "pgs_subs", "ass_subs", "bitrate")


def evaluate(rep: StreamReport, cap_4k_mbps: float, cap_1080_mbps: float) -> list[str]:
    issues: list[str] = []
    if rep.dolby_vision:
        issues.append("dolby_vision")
    codecs = [c.split(":", 1)[0] for c in rep.audio_codecs]
    if any(c == "truehd" for c in codecs):
        issues.append("truehd")
    if any(c in ("dca", "dts") or c.startswith("dts") for c in codecs):
        issues.append("dts")
    subs = {c.lower() for c in rep.embedded_subtitle_codecs}
    if subs & {"pgs", "hdmv_pgs_subtitle", "pgssub"}:
        issues.append("pgs_subs")
    if subs & {"ass", "ssa"}:
        issues.append("ass_subs")
    if rep.bitrate_kbps:
        cap = cap_4k_mbps if (rep.height or 0) > 1200 else cap_1080_mbps
        if rep.bitrate_kbps > cap * 1000:
            issues.append("bitrate")
    return issues


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def poll_imports(svc: Any) -> int:
    """Turn new import history records into pending flag rows. Returns how many were queued."""
    queued = 0
    for name, app in (("radarr", svc.radarr), ("sonarr", svc.sonarr)):
        if app is None:
            continue
        cur = svc.db.cursor(name)
        if cur is None:
            svc.db.set_cursor(name, 0, _iso(time.time()))
            continue
        try:
            records = await app.history_since(cur["last_date"])
        except ServiceError as exc:
            log.warning("tvcheck: %s history unavailable: %s", name, exc)
            continue
        last_id, last_date = int(cur["last_id"]), cur["last_date"]
        for rec in sorted(records, key=lambda r: int(r.get("id") or 0)):
            rid = int(rec.get("id") or 0)
            if rid <= last_id:
                continue
            last_id, last_date = rid, str(rec.get("date") or last_date)
            data = rec.get("data") or {}
            path = data.get("importedPath")
            if not path:
                continue
            fields: dict[str, Any] = {
                "service": name,
                "history_id": rid,
                "download_id": str(rec.get("downloadId") or "").lower() or None,
                "path": path,
                "file_id": int(data["fileId"]) if str(data.get("fileId") or "").isdigit() else None,
                "next_at": time.time() + 60,
            }
            if name == "radarr":
                fields["item_id"] = int(rec.get("movieId") or 0)
            else:
                fields["item_id"] = int(rec.get("seriesId") or 0)
                fields["episode_id"] = int(rec.get("episodeId") or 0)
            if svc.db.insert_flag(**fields):
                queued += 1
        svc.db.set_cursor(name, last_id, last_date)
    return queued


async def _describe(svc: Any, row: Any) -> dict[str, Any]:
    if row["service"] == "radarr":
        m = await svc.radarr.movie(row["item_id"])
        return {"kind": "movie", "title": m.get("title") or "", "year": m.get("year")}
    s = await svc.sonarr.series(row["item_id"])
    season = episode = None
    for e in await svc.sonarr.episodes(row["item_id"]):
        if int(e["id"]) == int(row["episode_id"] or -1):
            season, episode = e.get("seasonNumber"), e.get("episodeNumber")
    return {
        "kind": "episode",
        "title": s.get("title") or "",
        "year": s.get("year"),
        "season": season,
        "episode": episode,
    }


async def inspect_due(svc: Any) -> list[int]:
    """Inspect due rows. Returns ids of rows newly flagged (to be posted)."""
    cfg = svc.settings.tvcheck
    flagged: list[int] = []
    for row in svc.db.due_flags(time.time()):
        try:
            info = await _describe(svc, row)
            rep = await svc.mediaserver.inspect(
                info["kind"],
                info["title"],
                info.get("year"),
                svc.guard.map_path(row["path"]),
                season=info.get("season"),
                episode=info.get("episode"),
            )
        except ServiceError as exc:
            log.info("tvcheck: inspect deferred (%s)", exc)
            rep, info = None, {"title": row["title"] or ""}
        attempts = int(row["attempts"]) + 1
        if rep is None:
            if attempts >= cfg.max_attempts:
                svc.db.update_flag(row["id"], status="gave_up", attempts=attempts)
            else:
                delay = cfg.first_retry_minutes * 60 * (2 ** (attempts - 1))
                svc.db.update_flag(row["id"], attempts=attempts, next_at=time.time() + delay)
            continue
        issues = evaluate(rep, cfg.max_bitrate_mbps_4k, cfg.max_bitrate_mbps_1080p)
        svc.db.update_flag(
            row["id"],
            status="flagged" if issues else "ok",
            attempts=attempts,
            title=info.get("title"),
            year=info.get("year"),
            season=info.get("season"),
            episode=info.get("episode"),
            issues=json.dumps(
                {
                    "issues": issues,
                    "video": rep.video_codec,
                    "height": rep.height,
                    "bitrate_kbps": rep.bitrate_kbps,
                    "audio": rep.audio_codecs,
                    "subs": rep.embedded_subtitle_codecs,
                }
            ),
        )
        if issues:
            flagged.append(int(row["id"]))
    return flagged


async def execute_replace(svc: Any, flag_id: int, user_id: int, dry_run: bool) -> str:
    """Blocklist the release, delete the file (and its torrent per the shared rule), re-search."""
    row = svc.db.flag(flag_id)
    if row is None:
        raise ValueError("flag not found")
    label = row["title"] or "item"
    download_id = (row["download_id"] or "").upper()
    if row["service"] == "radarr":
        app = svc.radarr
        history = await app.history_for_movie(row["item_id"])
    else:
        app = svc.sonarr
        history = await app.history_for_series(row["item_id"])
    grab = next(
        (
            h
            for h in history
            if h.get("eventType") == GRAB_EVENT
            and str(h.get("downloadId") or "").upper() == download_id
        ),
        None,
    )
    torrents: list[str] = []
    if row["service"] == "radarr":
        plan = await plan_delete(svc, "movie", int(row["item_id"]))
        torrents = [t.hash for t in plan.deletable_torrents if t.hash == download_id.lower()]
    if dry_run:
        svc.db.audit(user_id, "tv_replace", label, 0, True, {"flag": flag_id})
        return f"[DRY RUN] would blocklist the release of {label}, delete the file and re-search"
    if grab is not None:
        await app.mark_failed(int(grab["id"]))
    if row["service"] == "radarr":
        if row["file_id"]:
            await svc.radarr.delete_movie_file(int(row["file_id"]))
        if torrents and svc.qbit is not None:
            await svc.qbit.delete(torrents, delete_files=True)
        await svc.radarr.search_movies([int(row["item_id"])])
    else:
        if row["file_id"]:
            await svc.sonarr.delete_episode_file(int(row["file_id"]))
        # Season packs are often shared: torrents are kept here; cleanup can remove them later.
        if row["episode_id"]:
            await svc.sonarr.search_episodes([int(row["episode_id"])])
    svc.db.update_flag(flag_id, status="replaced")
    svc.db.audit(user_id, "tv_replace", label, 0, False, {"flag": flag_id, "torrents": torrents})
    return f"Blocklisted the release of {label}, deleted the file and started a new search"
