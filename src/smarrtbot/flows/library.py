"""Read-only views of the Radarr/Sonarr library used by commands, tools and storage."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

Kind = Literal["movie", "series"]


@dataclass
class Title:
    kind: Kind
    id: int
    title: str
    year: int | None
    size: int
    seasons: dict[int, int] = field(default_factory=dict)  # season -> bytes (series only)
    has_files: bool = True

    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title

    def as_tool(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "year": self.year,
            "size_gb": round(self.size / 1024**3, 2),
        }
        if self.kind == "series":
            d["seasons"] = [
                {"season": n, "size_gb": round(b / 1024**3, 2)}
                for n, b in sorted(self.seasons.items())
                if b
            ]
        return d


def fold(text: str) -> str:
    """Lowercase, strip accents and punctuation: for forgiving title matching."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


async def library(svc: Any, kind: Kind | None = None) -> list[Title]:
    out: list[Title] = []
    if svc.radarr is not None and kind in (None, "movie"):
        for m in await svc.radarr.movies():
            out.append(
                Title(
                    "movie",
                    int(m["id"]),
                    str(m.get("title") or ""),
                    m.get("year"),
                    int(m.get("sizeOnDisk") or m.get("statistics", {}).get("sizeOnDisk") or 0),
                    has_files=bool(m.get("hasFile")),
                )
            )
    if svc.sonarr is not None and kind in (None, "series"):
        for s in await svc.sonarr.series_list():
            stats = s.get("statistics") or {}
            seasons = {
                int(x["seasonNumber"]): int((x.get("statistics") or {}).get("sizeOnDisk") or 0)
                for x in s.get("seasons", [])
            }
            out.append(
                Title(
                    "series",
                    int(s["id"]),
                    str(s.get("title") or ""),
                    s.get("year"),
                    int(stats.get("sizeOnDisk") or 0),
                    seasons=seasons,
                    has_files=bool(stats.get("episodeFileCount")),
                )
            )
    return out


def match_titles(titles: list[Title], query: str, limit: int = 5) -> list[Title]:
    q = fold(query)
    if not q:
        return []
    exact = [t for t in titles if fold(t.title) == q]
    if exact:
        return exact[:limit]
    words = q.split()
    hits = [t for t in titles if all(w in fold(t.title) for w in words)]
    hits.sort(key=lambda t: (len(t.title), -(t.size or 0)))
    return hits[:limit]


def largest(titles: list[Title], n: int = 10) -> list[Title]:
    return sorted((t for t in titles if t.size), key=lambda t: -t.size)[:n]
