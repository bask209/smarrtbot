"""Storage dashboard, daily snapshots, low-space alerts.

Sizes come from cheap APIs: Radarr/Sonarr sizeOnDisk, qBittorrent torrent sizes and the *arr
disk-space endpoint (or ``statvfs`` when the data folder is mounted). The scanner's cached,
inode-deduplicated numbers are added when available.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Any

from smarrtbot.flows.library import Title, largest, library


@dataclass
class StorageReport:
    total: int | None
    free: int | None
    movies: int
    tv: int
    torrents: int
    largest: list[Title]
    torrents_unique: int | None = None  # bytes only in torrents/ (not hardlinked into library)
    unique_total: int | None = None
    scan_age_s: float | None = None
    delta_7d: int | None = None  # change in *used* bytes
    delta_30d: int | None = None
    sources: list[str] = field(default_factory=list)

    @property
    def used(self) -> int | None:
        return None if self.total is None or self.free is None else self.total - self.free

    @property
    def free_percent(self) -> float | None:
        return None if not self.total or self.free is None else 100.0 * self.free / self.total

    def as_tool(self) -> dict[str, Any]:
        gb = 1024**3

        def g(v: int | None) -> float | None:
            return None if v is None else round(v / gb, 1)

        return {
            "disk_total_gb": g(self.total),
            "disk_used_gb": g(self.used),
            "disk_free_gb": g(self.free),
            "free_percent": None if self.free_percent is None else round(self.free_percent, 1),
            "movies_gb": g(self.movies),
            "tv_gb": g(self.tv),
            "torrents_gb": g(self.torrents),
            "torrents_not_in_library_gb": g(self.torrents_unique),
            "used_change_7d_gb": g(self.delta_7d),
            "used_change_30d_gb": g(self.delta_30d),
            "largest_titles": [t.as_tool() for t in self.largest],
        }


def _statvfs(path: str) -> tuple[int, int] | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize


async def _disk(svc: Any) -> tuple[int | None, int | None, str]:
    s = svc.settings.storage
    for root in (s.movies_root, s.torrents_root):
        res = _statvfs(root)  # cheap syscall; None when the folder is not mounted
        if res:
            return res[0], res[1], "statvfs"
    for app, root in ((svc.radarr, s.movies_root), (svc.sonarr, s.tv_root)):
        if app is None:
            continue
        best = None
        for d in await app.diskspace():
            p = str(d.get("path") or "")
            if (root == p or root.startswith(p.rstrip("/") + "/")) and (
                best is None or len(p) > len(best["path"])
            ):
                best = d
        if best:
            return int(best["totalSpace"]), int(best["freeSpace"]), f"{app.name} diskspace"
    return None, None, "unknown"


async def storage_report(svc: Any, top: int = 8) -> StorageReport:
    titles = await library(svc)
    movies = sum(t.size for t in titles if t.kind == "movie")
    tv = sum(t.size for t in titles if t.kind == "series")
    torrents = 0
    if svc.qbit is not None:
        for t in await svc.qbit.torrents():
            torrents += int(float(t.get("size") or 0) * float(t.get("progress") or 0))
    total, free, src = await _disk(svc)
    rep = StorageReport(total, free, movies, tv, torrents, largest(titles, top), sources=[src])
    scan = svc.scanner.result
    if scan is not None:
        troot = svc.settings.storage.torrents_root
        rs = scan.roots.get(troot)
        rep.torrents_unique = rs.single_link_bytes if rs else None
        rep.unique_total = scan.unique_bytes
        rep.scan_age_s = svc.scanner.age()
    if rep.used is not None:
        today = dt.date.today()
        for days, attr in ((7, "delta_7d"), (30, "delta_30d")):
            snap = svc.db.snapshot_on_or_before((today - dt.timedelta(days=days)).isoformat())
            if snap and snap["total"] and snap["free"] is not None:
                setattr(rep, attr, rep.used - (snap["total"] - snap["free"]))
    return rep


async def take_snapshot(svc: Any) -> StorageReport:
    rep = await storage_report(svc)
    svc.db.put_snapshot(
        dt.date.today().isoformat(),
        total=rep.total,
        free=rep.free,
        movies=rep.movies,
        tv=rep.tv,
        torrents=rep.torrents,
    )
    return rep


def low_space(svc: Any, rep: StorageReport) -> bool:
    s = svc.settings.storage
    if rep.free is None:
        return False
    pct = rep.free_percent or 0.0
    return pct < s.alert_free_percent or rep.free < s.alert_free_gb * 1024**3


async def check_alert(svc: Any) -> StorageReport | None:
    """Return a report when an alert should be sent now (once per low-space episode)."""
    rep = await storage_report(svc)
    is_low = low_space(svc, rep)
    was_low = bool(svc.db.kv_get("storage_alert_active", False))
    if is_low and not was_low:
        svc.db.kv_set("storage_alert_active", True)
        return rep
    if not is_low and was_low:
        svc.db.kv_set("storage_alert_active", False)
    return None
