"""Wiring: one object that holds every client and helper the flows need."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from smarrtbot.config import Settings, read_secret
from smarrtbot.db import Database
from smarrtbot.flows.actions import PendingActions
from smarrtbot.fsguard import PathGuard
from smarrtbot.logsafe import register_secret
from smarrtbot.mediaserver.base import MediaServer, NullMediaServer
from smarrtbot.scan import Scanner

if TYPE_CHECKING:
    from smarrtbot.llm.agent import Agent


@dataclass
class Services:
    settings: Settings
    db: Database
    guard: PathGuard
    scanner: Scanner
    radarr: Any = None
    sonarr: Any = None
    qbit: Any = None
    mediaserver: MediaServer = field(default_factory=NullMediaServer)
    agent: Agent | None = None
    pending: PendingActions | None = None

    def __post_init__(self) -> None:
        if self.pending is None:
            self.pending = PendingActions(self.db, self.settings.limits.pending_ttl_seconds)

    @property
    def dry_run(self) -> bool:
        return self.settings.dry_run

    async def aclose(self) -> None:
        for c in (self.radarr, self.sonarr, self.qbit, self.mediaserver):
            closer = getattr(c, "aclose", None)
            if closer:
                await closer()
        if self.agent is not None:
            await self.agent.aclose()


def build_guard(settings: Settings) -> PathGuard:
    s = settings.storage
    return PathGuard(s.read_roots, s.writable_roots, s.path_maps)


def build_services(settings: Settings, *, with_agent: bool = True) -> Services:
    from smarrtbot.clients.arr import RadarrClient, SonarrClient
    from smarrtbot.clients.qbit import QbitClient

    db = Database(settings.db_path)
    s = settings.storage
    svc = Services(
        settings=settings,
        db=db,
        guard=build_guard(settings),
        scanner=Scanner(
            s.read_roots,
            settings.data_dir / "scan_cache.json",
            min_interval_s=s.scan_min_interval_minutes * 60,
            ignore_dirs=s.ignore_dirs,
        ),
    )
    if settings.radarr.enabled:
        key = read_secret(settings.radarr.api_key_file, name="radarr api key")
        register_secret(key, "<radarr-key>")
        svc.radarr = RadarrClient(settings.radarr.url, key, timeout=settings.radarr.timeout)
    if settings.sonarr.enabled:
        key = read_secret(settings.sonarr.api_key_file, name="sonarr api key")
        register_secret(key, "<sonarr-key>")
        svc.sonarr = SonarrClient(settings.sonarr.url, key, timeout=settings.sonarr.timeout)
    if settings.qbittorrent.enabled:
        pw = read_secret(settings.qbittorrent.password_file, name="qbittorrent password")
        register_secret(pw, "<qbit-password>")
        svc.qbit = QbitClient(
            settings.qbittorrent.url,
            settings.qbittorrent.username,
            pw,
            timeout=settings.qbittorrent.timeout,
        )
    if settings.mediaserver.kind == "plex":
        from smarrtbot.mediaserver.plex import PlexServer

        token = read_secret(settings.mediaserver.token_file, name="plex token")
        register_secret(token, "<plex-token>")
        svc.mediaserver = PlexServer(
            settings.mediaserver.url, token, timeout=settings.mediaserver.timeout
        )
    if with_agent:
        from smarrtbot.llm.agent import build_agent

        svc.agent = build_agent(svc)
    return svc
