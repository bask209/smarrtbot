"""Radarr (v3 API, tested against 6.x) and Sonarr (v3 API, tested against 4.x) clients."""

from __future__ import annotations

from typing import Any

from smarrtbot.clients.http import HttpService

IMPORT_EVENT = "downloadFolderImported"
GRAB_EVENT = "grabbed"
EVENT_TYPE_IMPORTED = 3  # numeric id used by /history/since filters (same in both apps)


class ArrClient(HttpService):
    """Endpoints shared by Radarr and Sonarr."""

    def __init__(self, base_url: str, api_key: str, **kw: Any) -> None:
        super().__init__(base_url, headers={"X-Api-Key": api_key}, **kw)

    async def system_status(self) -> dict[str, Any]:
        return await self.get("/api/v3/system/status")

    async def diskspace(self) -> list[dict[str, Any]]:
        return await self.get("/api/v3/diskspace")

    async def free_space_for(self, path: str) -> int | None:
        """Free bytes on the disk that holds ``path`` (longest matching mount)."""
        best: dict[str, Any] | None = None
        for d in await self.diskspace():
            p = str(d.get("path") or "").rstrip("/") or "/"
            if (path == p or path.startswith(p.rstrip("/") + "/") or p == "/") and (
                best is None or len(p) > len(str(best.get("path") or ""))
            ):
                best = d
        return int(best["freeSpace"]) if best else None

    async def root_folders(self) -> list[dict[str, Any]]:
        return await self.get("/api/v3/rootfolder")

    async def quality_profiles(self) -> list[dict[str, Any]]:
        return await self.get("/api/v3/qualityprofile")

    async def quality_profile_schema(self) -> dict[str, Any]:
        return await self.get("/api/v3/qualityprofile/schema")

    async def create_quality_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/api/v3/qualityprofile", json=body)

    async def update_quality_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PUT", f"/api/v3/qualityprofile/{body['id']}", json=body)

    async def custom_formats(self) -> list[dict[str, Any]]:
        return await self.get("/api/v3/customformat")

    async def create_custom_format(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/api/v3/customformat", json=body)

    async def update_custom_format(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PUT", f"/api/v3/customformat/{body['id']}", json=body)

    async def notifications(self) -> list[dict[str, Any]]:
        return await self.get("/api/v3/notification")

    async def notification_schema(self) -> list[dict[str, Any]]:
        return await self.get("/api/v3/notification/schema")

    async def create_notification(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/api/v3/notification", json=body)

    async def update_notification(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PUT", f"/api/v3/notification/{body['id']}", json=body)

    async def test_notification(self, body: dict[str, Any]) -> Any:
        return await self.request("POST", "/api/v3/notification/test", json=body)

    async def history_by_download(self, download_id: str, **include: bool) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"downloadId": download_id.upper(), "pageSize": 1000}
        params.update({k: str(v).lower() for k, v in include.items()})
        page = await self.get("/api/v3/history", **params)
        return list(page.get("records", [])) if isinstance(page, dict) else []

    async def history_since(self, date_iso: str) -> list[dict[str, Any]]:
        return await self.get("/api/v3/history/since", date=date_iso, eventType=EVENT_TYPE_IMPORTED)

    async def mark_failed(self, history_id: int) -> Any:
        """Blocklist the release behind a 'grabbed' history record."""
        return await self.request("POST", f"/api/v3/history/failed/{int(history_id)}")

    async def queue(self, **include: bool) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"pageSize": 200}
        params.update({k: str(v).lower() for k, v in include.items()})
        page = await self.get("/api/v3/queue", **params)
        return list(page.get("records", [])) if isinstance(page, dict) else []

    async def command(self, name: str, **body: Any) -> dict[str, Any]:
        return await self.request("POST", "/api/v3/command", json={"name": name, **body})


class RadarrClient(ArrClient):
    name = "radarr"

    async def lookup(self, term: str) -> list[dict[str, Any]]:
        return await self.get("/api/v3/movie/lookup", term=term)

    async def lookup_tmdb(self, tmdb_id: int) -> dict[str, Any] | None:
        res = await self.get("/api/v3/movie/lookup/tmdb", tmdbId=int(tmdb_id))
        return res if isinstance(res, dict) and res.get("tmdbId") else None

    async def movies(self) -> list[dict[str, Any]]:
        return await self.get("/api/v3/movie")

    async def movie(self, movie_id: int) -> dict[str, Any]:
        return await self.get(f"/api/v3/movie/{int(movie_id)}")

    async def add_movie(
        self, lookup: dict[str, Any], profile_id: int, root_folder: str, search: bool = True
    ) -> dict[str, Any]:
        body = {
            "title": lookup["title"],
            "year": lookup.get("year"),
            "tmdbId": lookup["tmdbId"],
            "titleSlug": lookup.get("titleSlug"),
            "images": lookup.get("images", []),
            "qualityProfileId": int(profile_id),
            "rootFolderPath": root_folder,
            "monitored": True,
            "minimumAvailability": "released",
            "addOptions": {"searchForMovie": search},
        }
        return await self.request("POST", "/api/v3/movie", json=body)

    async def delete_movie(self, movie_id: int, delete_files: bool = True) -> None:
        await self.request(
            "DELETE",
            f"/api/v3/movie/{int(movie_id)}",
            params={"deleteFiles": str(delete_files).lower(), "addImportExclusion": "false"},
        )

    async def delete_movie_file(self, file_id: int) -> None:
        await self.request("DELETE", f"/api/v3/moviefile/{int(file_id)}")

    async def history_for_movie(self, movie_id: int) -> list[dict[str, Any]]:
        return await self.get("/api/v3/history/movie", movieId=int(movie_id))

    async def search_movies(self, movie_ids: list[int]) -> dict[str, Any]:
        return await self.command("MoviesSearch", movieIds=[int(i) for i in movie_ids])


class SonarrClient(ArrClient):
    name = "sonarr"

    async def lookup(self, term: str) -> list[dict[str, Any]]:
        return await self.get("/api/v3/series/lookup", term=term)

    async def lookup_tvdb(self, tvdb_id: int) -> dict[str, Any] | None:
        res = await self.lookup(f"tvdb:{int(tvdb_id)}")
        for r in res or []:
            if int(r.get("tvdbId") or 0) == int(tvdb_id):
                return r
        return None

    async def series_list(self) -> list[dict[str, Any]]:
        return await self.get("/api/v3/series")

    async def series(self, series_id: int) -> dict[str, Any]:
        return await self.get(f"/api/v3/series/{int(series_id)}")

    async def update_series(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PUT", f"/api/v3/series/{int(body['id'])}", json=body)

    async def add_series(
        self, lookup: dict[str, Any], profile_id: int, root_folder: str, search: bool = True
    ) -> dict[str, Any]:
        body = {
            "title": lookup["title"],
            "year": lookup.get("year"),
            "tvdbId": lookup["tvdbId"],
            "titleSlug": lookup.get("titleSlug"),
            "images": lookup.get("images", []),
            "seasons": lookup.get("seasons", []),
            "qualityProfileId": int(profile_id),
            "rootFolderPath": root_folder,
            "seasonFolder": True,
            "monitored": True,
            "addOptions": {"monitor": "all", "searchForMissingEpisodes": search},
        }
        return await self.request("POST", "/api/v3/series", json=body)

    async def delete_series(self, series_id: int, delete_files: bool = True) -> None:
        await self.request(
            "DELETE",
            f"/api/v3/series/{int(series_id)}",
            params={"deleteFiles": str(delete_files).lower(), "addImportListExclusion": "false"},
        )

    async def episode_files(self, series_id: int) -> list[dict[str, Any]]:
        return await self.get("/api/v3/episodefile", seriesId=int(series_id))

    async def delete_episode_files(self, file_ids: list[int]) -> None:
        if file_ids:
            await self.request(
                "DELETE",
                "/api/v3/episodefile/bulk",
                json={"episodeFileIds": [int(i) for i in file_ids]},
            )

    async def delete_episode_file(self, file_id: int) -> None:
        await self.request("DELETE", f"/api/v3/episodefile/{int(file_id)}")

    async def episodes(self, series_id: int) -> list[dict[str, Any]]:
        return await self.get("/api/v3/episode", seriesId=int(series_id))

    async def set_episodes_monitored(self, episode_ids: list[int], monitored: bool) -> None:
        if episode_ids:
            await self.request(
                "PUT",
                "/api/v3/episode/monitor",
                json={"episodeIds": [int(i) for i in episode_ids], "monitored": monitored},
            )

    async def history_for_series(
        self, series_id: int, season: int | None = None
    ) -> list[dict[str, Any]]:
        return await self.get(
            "/api/v3/history/series",
            seriesId=int(series_id),
            seasonNumber=season,
            includeEpisode="true",
        )

    async def search_episodes(self, episode_ids: list[int]) -> dict[str, Any]:
        return await self.command("EpisodeSearch", episodeIds=[int(i) for i in episode_ids])
