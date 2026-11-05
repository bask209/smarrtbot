"""Pluggable media-server adapters (Plex now; Jellyfin can implement the same interface)."""

from smarrtbot.mediaserver.base import MediaServer, NullMediaServer, StreamReport

__all__ = ["MediaServer", "NullMediaServer", "StreamReport"]
