"""Media-server interface used by the TV-friendly check."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

ItemKind = Literal["movie", "episode"]


@dataclass
class StreamReport:
    file: str
    video_codec: str | None = None
    height: int | None = None
    bitrate_kbps: int | None = None
    dolby_vision: bool = False
    hdr: str | None = None
    audio_codecs: list[str] = field(default_factory=list)
    embedded_subtitle_codecs: list[str] = field(default_factory=list)


class MediaServer(Protocol):
    name: str

    async def inspect(
        self,
        kind: ItemKind,
        title: str,
        year: int | None,
        file_path: str,
        season: int | None = None,
        episode: int | None = None,
    ) -> StreamReport | None:
        """Return stream details for the file, or None if the server has not indexed it yet."""
        ...

    async def ping(self) -> bool: ...


class NullMediaServer:
    name = "none"

    async def inspect(self, *args: object, **kwargs: object) -> StreamReport | None:
        return None

    async def ping(self) -> bool:
        return False
