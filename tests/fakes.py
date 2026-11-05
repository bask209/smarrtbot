"""In-memory fakes for Radarr, Sonarr, qBittorrent and the media server.

The fake data tree is real files in a tmp dir (so hardlink counts are real); services report
paths under ``/data`` exactly like the containers do, and the bot maps ``/data`` to the tmp dir.
All titles are invented.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from smarrtbot.clients.http import ServiceError
from smarrtbot.config import PathMap, Settings
from smarrtbot.db import Database
from smarrtbot.mediaserver.base import StreamReport
from smarrtbot.scan import Scanner
from smarrtbot.services import Services, build_guard

OWNER = 1001
ALICE = 2002
BOB = 3003
GROUP = -100123


class FakeArr:
    name = "arr"

    def __init__(self, env: Env) -> None:
        self.env = env
        self.calls: list[tuple[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.profiles = [{"id": 1, "name": "Any"}, {"id": 4, "name": "HD-1080p"}]
        self.roots: list[dict[str, Any]] = []
        self.queue_records: list[dict[str, Any]] = []
        self.custom_formats_list: list[dict[str, Any]] = []
        self.notifications_list: list[dict[str, Any]] = []
        self._next_id = 100
        self.fail_status: int | None = None

    def _nid(self) -> int:
        self._next_id += 1
        return self._next_id

    async def system_status(self) -> dict[str, Any]:
        if self.fail_status:
            raise ServiceError(self.name, "down", self.fail_status)
        return {"version": "9.9.9"}

    async def diskspace(self) -> list[dict[str, Any]]:
        st = os.statvfs(self.env.root)
        return [
            {"path": "/", "freeSpace": 1, "totalSpace": 2},
            {
                "path": "/data",
                "freeSpace": st.f_bavail * st.f_frsize,
                "totalSpace": st.f_blocks * st.f_frsize,
            },
        ]

    async def free_space_for(self, path: str) -> int | None:
        return self.env.free_bytes()

    async def root_folders(self) -> list[dict[str, Any]]:
        return self.roots

    async def quality_profiles(self) -> list[dict[str, Any]]:
        return self.profiles

    async def history_by_download(self, download_id: str, **_: Any) -> list[dict[str, Any]]:
        return [
            h for h in self.history if str(h.get("downloadId", "")).upper() == download_id.upper()
        ]

    async def history_since(self, date_iso: str) -> list[dict[str, Any]]:
        return [h for h in self.history if h.get("eventType") == "downloadFolderImported"]

    async def mark_failed(self, history_id: int) -> None:
        self.calls.append(("mark_failed", history_id))

    async def queue(self, **_: Any) -> list[dict[str, Any]]:
        return self.queue_records

    # setup-arr support
    async def custom_formats(self) -> list[dict[str, Any]]:
        return self.custom_formats_list

    async def create_custom_format(self, body: dict[str, Any]) -> dict[str, Any]:
        body = {**body, "id": self._nid()}
        self.custom_formats_list.append(body)
        self.calls.append(("create_cf", body["name"]))
        return body

    async def update_custom_format(self, body: dict[str, Any]) -> dict[str, Any]:
        self.custom_formats_list = [
            body if c["id"] == body["id"] else c for c in self.custom_formats_list
        ]
        self.calls.append(("update_cf", body["name"]))
        return body

    async def quality_profile_schema(self) -> dict[str, Any]:
        names = [
            "Unknown", "SDTV", "WEB 480p", "DVD", "HDTV-720p", "WEB 720p", "Bluray-720p",
            "HDTV-1080p", "WEB 1080p", "Bluray-1080p", "Remux-1080p", "HDTV-2160p", "WEB 2160p",
            "Bluray-2160p", "Remux-2160p",
        ]  # fmt: skip
        items = []
        for i, n in enumerate(names):
            if n.startswith("WEB"):
                items.append(
                    {"id": 1000 + i, "name": n, "items": [{"quality": {"id": i}}], "allowed": False}
                )
            else:
                items.append({"quality": {"id": i, "name": n}, "items": [], "allowed": False})
        return {"name": "", "upgradeAllowed": False, "cutoff": 0, "items": items,
                "minFormatScore": 0, "cutoffFormatScore": 0, "formatItems": []}  # fmt: skip

    async def create_quality_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        body = {**body, "id": self._nid()}
        self.profiles.append(body)
        self.calls.append(("create_profile", body["name"]))
        return body

    async def update_quality_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        self.profiles = [body if p["id"] == body["id"] else p for p in self.profiles]
        self.calls.append(("update_profile", body["name"]))
        return body

    async def notifications(self) -> list[dict[str, Any]]:
        return self.notifications_list

    async def notification_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "implementation": "Telegram",
                "name": "",
                "supportsOnGrab": True,
                "supportsOnDownload": True,
                "supportsOnUpgrade": True,
                "supportsOnHealthIssue": True,
                "supportsOnHealthRestored": True,
                "supportsOnManualInteractionRequired": True,
                "fields": [{"name": "botToken"}, {"name": "chatId"}, {"name": "sendSilently"}],
            }
        ]

    async def create_notification(self, body: dict[str, Any]) -> dict[str, Any]:
        body = {**body, "id": self._nid()}
        self.notifications_list.append(body)
        self.calls.append(("create_notification", body["name"]))
        return body

    async def update_notification(self, body: dict[str, Any]) -> dict[str, Any]:
        self.notifications_list = [
            body if n["id"] == body["id"] else n for n in self.notifications_list
        ]
        self.calls.append(("update_notification", body["name"]))
        return body

    async def test_notification(self, body: dict[str, Any]) -> None:
        self.calls.append(("test_notification", body["name"]))


class FakeRadarr(FakeArr):
    name = "radarr"

    def __init__(self, env: Env) -> None:
        super().__init__(env)
        self.movies_by_id: dict[int, dict[str, Any]] = {}
        self.lookup_results: list[dict[str, Any]] = []
        self.roots = [{"path": "/data/media/movies"}]

    async def lookup(self, term: str) -> list[dict[str, Any]]:
        return self.lookup_results

    async def lookup_tmdb(self, tmdb_id: int) -> dict[str, Any] | None:
        for r in self.lookup_results:
            if r["tmdbId"] == tmdb_id:
                return r
        return None

    async def movies(self) -> list[dict[str, Any]]:
        return list(self.movies_by_id.values())

    async def movie(self, movie_id: int) -> dict[str, Any]:
        if movie_id not in self.movies_by_id:
            raise ServiceError("radarr", "HTTP 404", 404)
        return self.movies_by_id[movie_id]

    async def add_movie(
        self, lookup: dict[str, Any], profile_id: int, root: str, search: bool = True
    ) -> dict[str, Any]:
        self.calls.append(("add_movie", (lookup["tmdbId"], profile_id, root)))
        return {"id": self._nid()}

    async def delete_movie(self, movie_id: int, delete_files: bool = True) -> None:
        self.calls.append(("delete_movie", movie_id))
        m = self.movies_by_id.pop(movie_id)
        if delete_files and m.get("movieFile"):
            self.env.unlink_service_path(m["movieFile"]["path"])

    async def delete_movie_file(self, file_id: int) -> None:
        self.calls.append(("delete_movie_file", file_id))
        for m in self.movies_by_id.values():
            mf = m.get("movieFile")
            if mf and mf["id"] == file_id:
                self.env.unlink_service_path(mf["path"])
                m["movieFile"] = None

    async def history_for_movie(self, movie_id: int) -> list[dict[str, Any]]:
        return [h for h in self.history if h.get("movieId") == movie_id]

    async def search_movies(self, ids: list[int]) -> dict[str, Any]:
        self.calls.append(("search_movies", ids))
        return {}

    async def movie_lookup_item(self, tmdb_id: int) -> None:  # pragma: no cover
        return None


class FakeSonarr(FakeArr):
    name = "sonarr"

    def __init__(self, env: Env) -> None:
        super().__init__(env)
        self.series_by_id: dict[int, dict[str, Any]] = {}
        self.files: list[dict[str, Any]] = []
        self.episodes_list: list[dict[str, Any]] = []
        self.lookup_results: list[dict[str, Any]] = []
        self.roots = [{"path": "/data/media/tv"}]

    async def lookup(self, term: str) -> list[dict[str, Any]]:
        return self.lookup_results

    async def lookup_tvdb(self, tvdb_id: int) -> dict[str, Any] | None:
        return next((r for r in self.lookup_results if r["tvdbId"] == tvdb_id), None)

    async def series_list(self) -> list[dict[str, Any]]:
        return list(self.series_by_id.values())

    async def series(self, series_id: int) -> dict[str, Any]:
        return self.series_by_id[series_id]

    async def update_series(self, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("update_series", body["id"]))
        self.series_by_id[body["id"]] = body
        return body

    async def add_series(
        self, lookup: dict[str, Any], profile_id: int, root: str, search: bool = True
    ) -> dict[str, Any]:
        self.calls.append(("add_series", lookup["tvdbId"]))
        return {"id": self._nid()}

    async def delete_series(self, series_id: int, delete_files: bool = True) -> None:
        self.calls.append(("delete_series", series_id))
        self.series_by_id.pop(series_id)
        for f in [f for f in self.files if f["seriesId"] == series_id]:
            self.env.unlink_service_path(f["path"])
            self.files.remove(f)

    async def episode_files(self, series_id: int) -> list[dict[str, Any]]:
        return [f for f in self.files if f["seriesId"] == series_id]

    async def delete_episode_files(self, ids: list[int]) -> None:
        self.calls.append(("delete_episode_files", sorted(ids)))
        for f in [f for f in self.files if f["id"] in ids]:
            self.env.unlink_service_path(f["path"])
            self.files.remove(f)

    async def delete_episode_file(self, file_id: int) -> None:
        await self.delete_episode_files([file_id])

    async def episodes(self, series_id: int) -> list[dict[str, Any]]:
        return [e for e in self.episodes_list if e["seriesId"] == series_id]

    async def set_episodes_monitored(self, ids: list[int], monitored: bool) -> None:
        self.calls.append(("set_episodes_monitored", (sorted(ids), monitored)))

    async def history_for_series(
        self, series_id: int, season: int | None = None
    ) -> list[dict[str, Any]]:
        out = [h for h in self.history if h.get("seriesId") == series_id]
        if season is not None:
            out = [h for h in out if (h.get("episode") or {}).get("seasonNumber") == season]
        return out

    async def search_episodes(self, ids: list[int]) -> dict[str, Any]:
        self.calls.append(("search_episodes", ids))
        return {}


class FakeQbit:
    name = "qbittorrent"

    def __init__(self, env: Env) -> None:
        self.env = env
        self.torrents_by_hash: dict[str, dict[str, Any]] = {}
        self.files_by_hash: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, Any]] = []

    def add(
        self, h: str, name: str, save_path: str, files: list[str], state: str = "stalledUP"
    ) -> None:
        sizes = [os.path.getsize(self.env.local(f"{save_path}/{f}")) for f in files]
        self.torrents_by_hash[h] = {
            "hash": h, "name": name, "size": sum(sizes), "progress": 1.0,
            "state": state, "save_path": save_path,
        }  # fmt: skip
        self.files_by_hash[h] = [{"name": f, "size": s} for f, s in zip(files, sizes, strict=True)]

    async def version(self) -> str:
        return "v5.0.0"

    async def torrents(self, hashes: list[str] | None = None) -> list[dict[str, Any]]:
        if hashes is None:
            return list(self.torrents_by_hash.values())
        return [self.torrents_by_hash[h] for h in hashes if h in self.torrents_by_hash]

    async def files(self, h: str) -> list[dict[str, Any]]:
        return self.files_by_hash.get(h, [])

    async def delete(self, hashes: list[str], delete_files: bool = True) -> None:
        self.calls.append(("delete", sorted(hashes)))
        for h in hashes:
            t = self.torrents_by_hash.pop(h)
            for f in self.files_by_hash.pop(h):
                if delete_files:
                    self.env.unlink_service_path(f"{t['save_path']}/{f['name']}")


@dataclass
class FakeMediaServer:
    name: str = "fake"
    reports: dict[str, StreamReport | None] = field(default_factory=dict)
    calls: int = 0

    async def inspect(
        self,
        kind: str,
        title: str,
        year: Any,
        file_path: str,
        season: Any = None,
        episode: Any = None,
    ) -> StreamReport | None:
        self.calls += 1
        return self.reports.get(os.path.basename(file_path))

    async def ping(self) -> bool:
        return True


class Env:
    """A tmp data tree that the fakes treat as '/data'."""

    def __init__(self, tmp: Path) -> None:
        self.root = str(tmp.resolve() / "data")
        for d in ("media/movies", "media/tv", "torrents/radarr", "torrents/tv-sonarr"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        self.freed_offset = 0

    def local(self, service_path: str) -> str:
        assert service_path.startswith("/data/")
        return self.root + service_path[len("/data") :]

    def write(self, service_path: str, size: int) -> str:
        p = self.local(service_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"\0" * size)
        return p

    def link(self, src_service: str, dst_service: str) -> None:
        dst = self.local(dst_service)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.link(self.local(src_service), dst)

    def unlink_service_path(self, service_path: str) -> None:
        p = self.local(service_path)
        st = os.lstat(p)
        if st.st_nlink == 1:
            self.freed_offset += st.st_size
        os.unlink(p)

    def free_bytes(self) -> int:
        # Deterministic "free space": a big constant plus whatever the fakes freed.
        return 500 * 1024**3 + self.freed_offset


def make_services(tmp: Path, **overrides: Any) -> tuple[Services, Env]:
    env = Env(tmp)
    data = {
        "dry_run": False,
        "data_dir": str(tmp / "state"),
        "telegram": {"owner_id": OWNER, "allowed_group_ids": [GROUP]},
        "storage": {
            "movies_root": env.root + "/media/movies",
            "tv_root": env.root + "/media/tv",
            "torrents_root": env.root + "/torrents",
            "writable_roots": [env.root + "/torrents"],
            "path_maps": [PathMap(src="/data", dst=env.root).model_dump()],
            "orphan_min_age_minutes": 0,
        },
        "limits": {"daily_delete_cap_gb": 0},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k] = {**data[k], **v}  # type: ignore[dict-item]
        else:
            data[k] = v
    settings = Settings(**data)
    db = Database(":memory:")
    s = settings.storage
    svc = Services(
        settings=settings,
        db=db,
        guard=build_guard(settings),
        scanner=Scanner(s.read_roots, tmp / "state" / "scan.json", min_interval_s=0),
    )
    svc.radarr = FakeRadarr(env)
    svc.sonarr = FakeSonarr(env)
    svc.qbit = FakeQbit(env)
    svc.mediaserver = FakeMediaServer()
    db.set_user_status(ALICE, "approved", OWNER)
    db.set_user_status(BOB, "approved", OWNER)
    return svc, env


MB = 1024 * 1024


def add_movie(
    svc: Services,
    env: Env,
    movie_id: int,
    title: str,
    torrent_hash: str | None,
    *,
    size: int = 3 * MB,
    extra_torrent_files: dict[str, int] | None = None,
    state: str = "stalledUP",
) -> None:
    """A movie imported from a torrent (hardlinked), like Radarr does it."""
    folder = f"{title} (2001)"
    tpath = f"/data/torrents/radarr/{title}.2001.1080p"
    mpath = f"/data/media/movies/{folder}/{title} (2001).mkv"
    env.write(f"{tpath}/{title}.mkv", size)
    env.link(f"{tpath}/{title}.mkv", mpath)
    files = [f"{title}.2001.1080p/{title}.mkv"]
    for name, sz in (extra_torrent_files or {}).items():
        env.write(f"{tpath}/{name}", sz)
        files.append(f"{title}.2001.1080p/{name}")
    svc.radarr.movies_by_id[movie_id] = {
        "id": movie_id, "title": title, "year": 2001, "path": f"/data/media/movies/{folder}",
        "hasFile": True, "sizeOnDisk": size,
        "movieFile": {"id": movie_id * 10, "path": mpath, "size": size},
    }  # fmt: skip
    if torrent_hash:
        svc.radarr.history += [
            {"id": movie_id * 100, "eventType": "grabbed", "movieId": movie_id, "downloadId": torrent_hash.upper()},
            {"id": movie_id * 100 + 1, "eventType": "downloadFolderImported", "movieId": movie_id,
             "downloadId": torrent_hash.upper(),
             "data": {"importedPath": mpath, "droppedPath": f"{tpath}/{title}.mkv", "fileId": str(movie_id * 10)}},
        ]  # fmt: skip
        svc.qbit.add(
            torrent_hash, f"{title}.2001.1080p", "/data/torrents/radarr", files, state=state
        )
