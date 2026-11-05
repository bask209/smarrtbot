"""Hardlink-aware delete of a movie, a whole series or one season.

Library files are hardlinks to the files a torrent is seeding, so deleting in Radarr/Sonarr
alone frees nothing while the torrent exists. Order of operations:

1. delete the title (and its files) through Radarr or Sonarr;
2. then delete each matching torrent *and its files* through qBittorrent.

Matching torrents come only from the *arr history ``downloadId`` (the torrent hash) for the
target. A torrent is deleted only when **all** of its content belongs to the target:

* history: every record with that downloadId must point at the target movie / series / season;
* hardlinks (when the data folders are mounted): no torrent file may have a link outside the
  set of target media files.

Otherwise the torrent is kept and reported ("shared, kept, X not freed"). Nothing is guessed: no
history match means "no torrent found".
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from smarrtbot.clients.arr import GRAB_EVENT, IMPORT_EVENT
from smarrtbot.clients.qbit import SEEDING_STATES
from smarrtbot.flows.actions import Proposal
from smarrtbot.fsguard import PathRefused
from smarrtbot.util import human_bytes, untrusted

log = logging.getLogger(__name__)

TargetKind = Literal["movie", "series", "season"]


class DeleteError(Exception):
    pass


@dataclass
class MediaFile:
    path: str
    size: int
    file_id: int


@dataclass
class TorrentPlan:
    hash: str
    name: str
    size: int
    state: str
    seeding: bool
    deletable: bool
    reasons: list[str] = field(default_factory=list)
    files: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class DeletePlan:
    kind: TargetKind
    arr_id: int
    season: int | None
    title: str
    year: int | None
    media_files: list[MediaFile]
    torrents: list[TorrentPlan]
    media_bytes: int
    expected_freed: int | None
    hardlink_check: bool
    root_path: str
    notes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        base = f"{self.title} ({self.year})" if self.year else self.title
        return (
            f"{base} S{self.season:02d}"
            if self.kind == "season" and self.season is not None
            else base
        )

    @property
    def deletable_torrents(self) -> list[TorrentPlan]:
        return [t for t in self.torrents if t.deletable]

    @property
    def kept_torrents(self) -> list[TorrentPlan]:
        return [t for t in self.torrents if not t.deletable]

    @property
    def headline_bytes(self) -> int:
        """Bytes shown on the confirm button: the expected freed space when we can compute it."""
        if self.expected_freed is not None:
            return self.expected_freed
        return self.media_bytes + sum(t.size for t in self.deletable_torrents)

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "arr_id": self.arr_id,
            "season": self.season,
            "title": self.title,
            "year": self.year,
            "file_ids": sorted(f.file_id for f in self.media_files),
            "media_bytes": self.media_bytes,
            "torrent_hashes": sorted(t.hash for t in self.deletable_torrents),
            "expected_freed": self.expected_freed,
        }

    def to_summary(self) -> dict[str, Any]:
        return {
            "target": self.label,
            "media_files": len(self.media_files),
            "media_size": human_bytes(self.media_bytes),
            "torrents_to_delete": [
                {"name": untrusted(t.name, 100), "size": human_bytes(t.size), "seeding": t.seeding}
                for t in self.deletable_torrents
            ],
            "torrents_kept_shared": [
                {"name": untrusted(t.name, 100), "reasons": t.reasons} for t in self.kept_torrents
            ],
            "expected_freed": human_bytes(self.expected_freed),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------------------------


def _record_belongs(rec: dict[str, Any], kind: TargetKind, arr_id: int, season: int | None) -> bool:
    if kind == "movie":
        return int(rec.get("movieId") or -1) == arr_id
    if int(rec.get("seriesId") or -1) != arr_id:
        return False
    if kind == "series":
        return True
    ep = rec.get("episode") or {}
    sn = ep.get("seasonNumber")
    return sn is not None and int(sn) == season


async def plan_delete(
    svc: Any, kind: TargetKind, arr_id: int, season: int | None = None
) -> DeletePlan:
    if kind == "season" and season is None:
        raise DeleteError("season number required")
    notes: list[str] = []
    if kind == "movie":
        if svc.radarr is None:
            raise DeleteError("Radarr is not configured")
        app = svc.radarr
        m = await app.movie(arr_id)
        title, year = str(m.get("title") or ""), m.get("year")
        root_path = str(m.get("path") or svc.settings.storage.movies_root)
        mf = m.get("movieFile")
        files = [MediaFile(mf["path"], int(mf.get("size") or 0), int(mf["id"]))] if mf else []
        history = await app.history_for_movie(arr_id)
    else:
        if svc.sonarr is None:
            raise DeleteError("Sonarr is not configured")
        app = svc.sonarr
        s = await app.series(arr_id)
        title, year = str(s.get("title") or ""), s.get("year")
        root_path = str(s.get("path") or svc.settings.storage.tv_root)
        if kind == "season" and season not in {
            int(x["seasonNumber"]) for x in s.get("seasons", [])
        }:
            raise DeleteError(f"season {season} not found")
        efs = await app.episode_files(arr_id)
        files = [
            MediaFile(f["path"], int(f.get("size") or 0), int(f["id"]))
            for f in efs
            if kind == "series" or int(f.get("seasonNumber", -1)) == season
        ]
        history = await app.history_for_series(arr_id, season if kind == "season" else None)
        if kind == "season":
            history = [r for r in history if _record_belongs(r, kind, arr_id, season)]

    download_ids: list[str] = []
    for rec in history:
        if rec.get("eventType") in (IMPORT_EVENT, GRAB_EVENT) and rec.get("downloadId"):
            d = str(rec["downloadId"]).lower()
            if d not in download_ids:
                download_ids.append(d)

    # Hardlink view of the target media files (needs the data folders mounted read-only).
    hardlink_check = True
    media_inodes: dict[tuple[int, int], int] = {}  # (dev, inode) -> links inside the target
    inode_info: dict[tuple[int, int], tuple[int, int]] = {}  # -> (size, nlink)
    for f in files:
        try:
            st = svc.guard.stat_file(svc.guard.map_path(f.path))
        except PathRefused as exc:
            hardlink_check = False
            notes.append(f"hardlink check unavailable for a media file ({exc})")
            continue
        if st is None:
            hardlink_check = False
            notes.append("a media file is not visible to the bot (check the data mount)")
            continue
        key = (st.dev, st.inode)
        media_inodes[key] = media_inodes.get(key, 0) + 1
        inode_info[key] = (st.size, st.nlink)

    torrents: list[TorrentPlan] = []
    if svc.qbit is not None and download_ids:
        found = {t["hash"].lower(): t for t in await svc.qbit.torrents(download_ids)}
        for d in download_ids:
            t = found.get(d)
            if t is None:
                continue  # grabbed earlier but already removed from the client
            plan = TorrentPlan(
                hash=d,
                name=str(t.get("name") or d),
                size=int(t.get("size") or t.get("total_size") or 0),
                state=str(t.get("state") or ""),
                seeding=str(t.get("state") or "") in SEEDING_STATES,
                deletable=True,
            )
            # Rule 1: every history record for this torrent must belong to the target.
            records = await app.history_by_download(
                d, **({"includeEpisode": True} if kind != "movie" else {})
            )
            others = [r for r in records if not _record_belongs(r, kind, arr_id, season)]
            if others:
                plan.deletable = False
                plan.reasons.append(f"also used by {len(others)} other history item(s)")
            # Rule 2: no torrent file may be hardlinked to anything outside the target.
            save_path = str(t.get("save_path") or "").rstrip("/")
            for tf in await svc.qbit.files(d):
                fpath = svc.guard.map_path(f"{save_path}/{tf['name']}")
                plan.files.append((fpath, int(tf.get("size") or 0)))
                if not hardlink_check:
                    continue
                try:
                    st = svc.guard.stat_file(fpath)
                except PathRefused as exc:
                    plan.deletable = False
                    plan.reasons.append(f"torrent file outside allowed roots ({exc})")
                    continue
                if st is None:
                    continue
                key = (st.dev, st.inode)
                inode_info[key] = (st.size, st.nlink)
                known_links = 1 + media_inodes.get(key, 0)
                if st.nlink > known_links:
                    plan.deletable = False
                    plan.reasons.append("a file is hardlinked outside this title")
            torrents.append(plan)

    expected: int | None = None
    if hardlink_check:
        links_deleted: dict[tuple[int, int], int] = dict(media_inodes)
        for tp in torrents:
            if not tp.deletable:
                continue
            for fpath, _size in tp.files:
                try:
                    st = svc.guard.stat_file(fpath)
                except PathRefused:
                    st = None
                if st is not None:
                    key = (st.dev, st.inode)
                    links_deleted[key] = links_deleted.get(key, 0) + 1
        expected = sum(inode_info[k][0] for k, n in links_deleted.items() if n >= inode_info[k][1])
    if not download_ids or not torrents:
        notes.append("no torrent found")
    return DeletePlan(
        kind=kind,
        arr_id=arr_id,
        season=season,
        title=untrusted(title, 120),
        year=year,
        media_files=files,
        torrents=torrents,
        media_bytes=sum(f.size for f in files),
        expected_freed=expected,
        hardlink_check=hardlink_check,
        root_path=root_path,
        notes=notes,
    )


# ---------------------------------------------------------------------------------------------
# limits
# ---------------------------------------------------------------------------------------------


def check_limits(svc: Any, user_id: int, planned_bytes: int, action: str = "delete") -> str | None:
    """Return a refusal reason, or None if the delete may proceed."""
    lim = svc.settings.limits
    is_owner = user_id == svc.settings.telegram.owner_id
    if action == "delete" and not (is_owner and lim.owner_exempt_from_rate_limit):
        n = svc.db.count_actions(user_id, ["delete"], time.time() - 3600)
        if n >= lim.deletes_per_hour:
            return f"rate limit: {lim.deletes_per_hour} deletes per hour"
    cap = int(lim.daily_delete_cap_gb * 1024**3)
    if cap > 0:
        day_start = time.time() - 86400
        used = svc.db.bytes_deleted_since(day_start)
        if used + planned_bytes > cap:
            return f"daily cap: {human_bytes(used)} deleted in the last 24h, cap {human_bytes(cap)}"
    return None


# ---------------------------------------------------------------------------------------------
# proposal and execution
# ---------------------------------------------------------------------------------------------


async def propose_delete(
    svc: Any,
    kind: TargetKind,
    arr_id: int,
    season: int | None,
    requester_id: int,
    chat_id: int,
) -> tuple[Proposal, DeletePlan]:
    plan = await plan_delete(svc, kind, arr_id, season)
    refusal = check_limits(svc, requester_id, plan.headline_bytes)
    if refusal:
        raise DeleteError(refusal)
    nonce = svc.pending.create("delete", requester_id, chat_id, plan.to_payload())
    return (
        Proposal(
            nonce=nonce,
            kind="delete",
            label=plan.label,
            size=plan.headline_bytes,
            details=plan_details(plan),
            summary={"status": "awaiting_user_confirmation", **plan.to_summary()},
        ),
        plan,
    )


def plan_details(plan: DeletePlan) -> list[str]:
    """Language-neutral detail codes rendered by the bot (see i18n 'detail.*')."""
    out = [f"media:{len(plan.media_files)}:{plan.media_bytes}"]
    for t in plan.deletable_torrents:
        out.append(f"torrent_delete:{t.size}:{int(t.seeding)}")
    for t in plan.kept_torrents:
        out.append(f"torrent_kept:{t.size}")
    if not plan.torrents:
        out.append("no_torrent")
    if not plan.hardlink_check:
        out.append("no_hardlink_check")
    return out


@dataclass
class DeleteResult:
    label: str
    dry_run: bool
    measured_freed: int | None
    expected_freed: int | None
    torrents_deleted: list[str]
    torrents_seeding: list[str]
    torrents_kept: list[tuple[str, int]]
    no_torrent: bool
    mismatch: bool
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def execute_delete(
    svc: Any, payload: dict[str, Any], user_id: int, dry_run: bool
) -> DeleteResult:
    kind: TargetKind = payload["kind"]
    arr_id, season = int(payload["arr_id"]), payload.get("season")
    fresh = await plan_delete(svc, kind, arr_id, season)
    # The stored payload is authoritative for *what* was confirmed; refuse if it drifted.
    if sorted(f.file_id for f in fresh.media_files) != list(payload["file_ids"]):
        raise DeleteError("the library changed since the confirmation; please ask again")
    confirmed = set(payload["torrent_hashes"])
    to_delete = [t for t in fresh.deletable_torrents if t.hash in confirmed]
    kept = [(t.name, t.size) for t in fresh.kept_torrents]
    kept += [(t.name, t.size) for t in fresh.deletable_torrents if t.hash not in confirmed]
    refusal = check_limits(svc, user_id, fresh.headline_bytes)
    if refusal:
        raise DeleteError(refusal)

    app = svc.radarr if kind == "movie" else svc.sonarr
    result = DeleteResult(
        label=fresh.label,
        dry_run=dry_run,
        measured_freed=None,
        expected_freed=fresh.expected_freed,
        torrents_deleted=[t.name for t in to_delete],
        torrents_seeding=[t.name for t in to_delete if t.seeding],
        torrents_kept=kept,
        no_torrent=not fresh.torrents,
        mismatch=False,
    )
    if dry_run:
        svc.db.audit(user_id, "delete", fresh.label, 0, True, fresh.to_summary())
        return result

    before = await app.free_space_for(fresh.root_path)
    # Step 1: the title and its files through Radarr/Sonarr.
    if kind == "movie":
        await svc.radarr.delete_movie(arr_id, delete_files=True)
    elif kind == "series":
        await svc.sonarr.delete_series(arr_id, delete_files=True)
    else:
        await svc.sonarr.delete_episode_files([f.file_id for f in fresh.media_files])
        s = await svc.sonarr.series(arr_id)
        for x in s.get("seasons", []):
            if int(x["seasonNumber"]) == season:
                x["monitored"] = False
        await svc.sonarr.update_series(s)
        eps = [
            int(e["id"])
            for e in await svc.sonarr.episodes(arr_id)
            if int(e.get("seasonNumber", -1)) == season
        ]
        await svc.sonarr.set_episodes_monitored(eps, False)
    # Step 2: then the matching torrents and their files through qBittorrent.
    if to_delete:
        try:
            await svc.qbit.delete([t.hash for t in to_delete], delete_files=True)
        except Exception as exc:
            result.errors.append(f"title deleted, but the torrent delete failed: {exc}")
    # Measure: qBittorrent deletes asynchronously, so poll briefly.
    after = before
    target = fresh.expected_freed or 0
    for _ in range(8):
        await asyncio.sleep(2)
        after = await app.free_space_for(fresh.root_path)
        if before is None or after is None or after - before >= target * 0.95:
            break
    if before is not None and after is not None:
        result.measured_freed = after - before
        if fresh.expected_freed is not None:
            diff = abs(result.measured_freed - fresh.expected_freed)
            result.mismatch = diff > max(1024**3, 0.1 * fresh.expected_freed)
    freed = result.measured_freed if result.measured_freed is not None else (target or 0)
    svc.db.audit(
        user_id,
        "delete",
        fresh.label,
        max(0, freed),
        False,
        {**fresh.to_summary(), "measured": result.measured_freed, "errors": result.errors},
    )
    return result
