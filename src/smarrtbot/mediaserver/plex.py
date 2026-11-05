"""Plex adapter. Finds an item by title and matches the imported file by basename."""

from __future__ import annotations

import posixpath
from typing import Any

from smarrtbot.clients.http import HttpService, ServiceError
from smarrtbot.mediaserver.base import ItemKind, StreamReport

_TYPE_MOVIE, _TYPE_SHOW = 1, 2


class PlexServer(HttpService):
    name = "plex"

    def __init__(self, base_url: str, token: str, **kw: Any) -> None:
        super().__init__(
            base_url, headers={"X-Plex-Token": token, "Accept": "application/json"}, **kw
        )

    async def ping(self) -> bool:
        try:
            await self.get("/identity")
            return True
        except ServiceError:
            return False

    async def _sections(self, plex_type: str) -> list[str]:
        data = await self.get("/library/sections")
        dirs = (data or {}).get("MediaContainer", {}).get("Directory", [])
        return [str(d["key"]) for d in dirs if d.get("type") == plex_type]

    async def _items(self, section: str, plex_type: int, title: str) -> list[dict[str, Any]]:
        data = await self.get(f"/library/sections/{section}/all", type=plex_type, title=title)
        return list((data or {}).get("MediaContainer", {}).get("Metadata", []))

    async def _metadata(self, rating_key: str) -> dict[str, Any] | None:
        data = await self.get(f"/library/metadata/{rating_key}")
        items = (data or {}).get("MediaContainer", {}).get("Metadata", [])
        return items[0] if items else None

    async def inspect(
        self,
        kind: ItemKind,
        title: str,
        year: int | None,
        file_path: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> StreamReport | None:
        base = posixpath.basename(file_path)
        if kind == "movie":
            for section in await self._sections("movie"):
                for item in await self._items(section, _TYPE_MOVIE, title):
                    if year and item.get("year") and abs(int(item["year"]) - int(year)) > 1:
                        continue
                    meta = await self._metadata(str(item["ratingKey"]))
                    report = parse_streams(meta, base) if meta else None
                    if report:
                        return report
            return None
        for section in await self._sections("show"):
            for show in await self._items(section, _TYPE_SHOW, title):
                leaves = await self.get(f"/library/metadata/{show['ratingKey']}/allLeaves")
                for ep in (leaves or {}).get("MediaContainer", {}).get("Metadata", []):
                    if season is not None and ep.get("parentIndex") not in (None, season):
                        continue
                    if episode is not None and ep.get("index") not in (None, episode):
                        continue
                    if not _has_part(ep, base):
                        continue
                    meta = await self._metadata(str(ep["ratingKey"]))
                    report = parse_streams(meta, base) if meta else None
                    if report:
                        return report
        return None


def _has_part(item: dict[str, Any], basename: str) -> bool:
    for media in item.get("Media", []) or []:
        for part in media.get("Part", []) or []:
            if posixpath.basename(str(part.get("file", ""))) == basename:
                return True
    return False


def parse_streams(meta: dict[str, Any], basename: str) -> StreamReport | None:
    """Build a report from Plex metadata for the part whose file matches ``basename``."""
    for media in meta.get("Media", []) or []:
        for part in media.get("Part", []) or []:
            if posixpath.basename(str(part.get("file", ""))) != basename:
                continue
            streams = part.get("Stream", []) or []
            if not streams:
                return None  # not analysed yet: retry later
            rep = StreamReport(file=str(part.get("file")))
            rep.bitrate_kbps = _int(media.get("bitrate"))
            rep.height = _int(media.get("height"))
            for s in streams:
                st = _int(s.get("streamType"))
                codec = str(s.get("codec") or "").lower()
                if st == 1 and rep.video_codec is None:
                    rep.video_codec = codec
                    rep.height = rep.height or _int(s.get("height"))
                    if s.get("DOVIPresent") or s.get("DOVIProfile") is not None:
                        rep.dolby_vision = True
                    cs = str(s.get("colorTrc") or "").lower()
                    if cs in ("smpte2084", "arib-std-b67"):
                        rep.hdr = "HDR10" if cs == "smpte2084" else "HLG"
                elif st == 2:
                    profile = str(s.get("profile") or "").lower()
                    rep.audio_codecs.append(f"{codec}:{profile}" if profile else codec)
                elif st == 3 and _embedded(s):
                    rep.embedded_subtitle_codecs.append(codec)
            return rep
    return None


def _embedded(stream: dict[str, Any]) -> bool:
    # Embedded streams carry a container index; sidecar files (e.g. Bazarr SRTs) carry a key.
    return stream.get("index") is not None and not str(stream.get("key") or "").startswith(
        "/library/streams/"
    )


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
