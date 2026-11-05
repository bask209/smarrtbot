"""Configuration: a YAML file plus environment overrides, secrets read from mounted files.

Precedence (highest first): environment variables (``SMARRTBOT_`` prefix, ``__`` for nesting),
then the YAML file named by ``SMARRTBOT_CONFIG`` (default ``/config/config.yaml``).
Secrets are never values in config: config only holds the *path* of a secret file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

DEFAULT_CONFIG_PATH = "/config/config.yaml"


class SecretError(RuntimeError):
    """A secret file is missing or empty. The message never contains secret content."""


def read_secret(path: Path | str | None, *, name: str = "secret") -> str:
    if path is None:
        raise SecretError(f"{name}: no secret file configured")
    p = Path(path)
    try:
        value = p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SecretError(f"{name}: cannot read secret file {p} ({exc.strerror})") from None
    if not value:
        raise SecretError(f"{name}: secret file {p} is empty")
    return value


class TelegramSettings(BaseModel):
    token_file: Path = Path("/run/secrets/telegram_token")
    owner_id: int = 0
    allowed_group_ids: list[int] = Field(default_factory=list)
    # Where automatic posts (TV-check flags, storage alerts) go. Defaults to the first group.
    notify_chat_id: int | None = None

    def notify_target(self) -> int | None:
        if self.notify_chat_id is not None:
            return self.notify_chat_id
        return self.allowed_group_ids[0] if self.allowed_group_ids else None


class ArrSettings(BaseModel):
    url: str = ""
    api_key_file: Path | None = None
    # Name or numeric id. Empty: the setup-arr profile if it exists, else HD-1080p, else first.
    quality_profile: str | int | None = None
    root_folder: str | None = None
    timeout: float = 20.0

    @property
    def enabled(self) -> bool:
        return bool(self.url)


class QbitSettings(BaseModel):
    url: str = ""
    username: str = "admin"
    password_file: Path | None = None
    timeout: float = 20.0

    @property
    def enabled(self) -> bool:
        return bool(self.url)


class MediaServerSettings(BaseModel):
    kind: Literal["none", "plex"] = "none"
    url: str = ""
    token_file: Path | None = None
    timeout: float = 20.0


class GeminiSettings(BaseModel):
    keys_file: Path = Path("/run/secrets/gemini_keys")
    base_url: str = "https://generativelanguage.googleapis.com"
    pinned_model: str | None = None
    max_consecutive_failures: int = 5
    timeout: float = 45.0
    max_tool_rounds: int = 4
    temperature: float = 0.2
    memory_turns: int = 12
    summarize_after_turns: int = 24


class LimitSettings(BaseModel):
    deletes_per_hour: int = 5
    owner_exempt_from_rate_limit: bool = True
    daily_delete_cap_gb: float = 500.0
    pending_ttl_seconds: int = 300


class PathMap(BaseModel):
    """Translate a path as a service sees it (src) into the bot container's view (dst)."""

    src: str
    dst: str


class StorageSettings(BaseModel):
    movies_root: str = "/data/media/movies"
    tv_root: str = "/data/media/tv"
    torrents_root: str = "/data/torrents"
    # Only these may ever be written by the guarded filesystem delete. Media is never in here.
    writable_roots: list[str] = Field(default_factory=lambda: ["/data/torrents"])
    path_maps: list[PathMap] = Field(default_factory=list)
    alert_free_percent: float = 10.0
    alert_free_gb: float = 100.0
    alert_check_minutes: int = 30
    weekly_summary: bool = False
    scan_interval_hours: float = 24.0
    scan_min_interval_minutes: float = 60.0
    orphan_min_age_minutes: float = 120.0
    ignore_dirs: list[str] = Field(default_factory=lambda: ["incomplete", ".incomplete"])
    suspicious_extensions: list[str] = Field(
        default_factory=lambda: [".exe", ".scr", ".bat", ".cmd", ".com", ".lnk", ".msi", ".iso"]
    )

    @field_validator("movies_root", "tv_root", "torrents_root")
    @classmethod
    def _absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("storage roots must be absolute paths")
        return v.rstrip("/") or "/"

    @property
    def read_roots(self) -> list[str]:
        return [self.movies_root, self.tv_root, self.torrents_root]


class TvCheckSettings(BaseModel):
    enabled: bool = True
    interval_minutes: float = 5.0
    max_bitrate_mbps_4k: float = 25.0
    max_bitrate_mbps_1080p: float = 12.0
    max_attempts: int = 6
    first_retry_minutes: float = 5.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SMARRTBOT_", env_nested_delimiter="__", extra="ignore"
    )

    dry_run: bool = True
    data_dir: Path = Path("/data/state")
    log_level: str = "INFO"
    health_host: str = "0.0.0.0"  # noqa: S104 - inside the container; publish selectively
    health_port: int = 8080

    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    radarr: ArrSettings = Field(default_factory=ArrSettings)
    sonarr: ArrSettings = Field(default_factory=ArrSettings)
    qbittorrent: QbitSettings = Field(default_factory=QbitSettings)
    mediaserver: MediaServerSettings = Field(default_factory=MediaServerSettings)
    gemini: GeminiSettings = Field(default_factory=GeminiSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    tvcheck: TvCheckSettings = Field(default_factory=TvCheckSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        path = os.environ.get("SMARRTBOT_CONFIG", DEFAULT_CONFIG_PATH)
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if Path(path).is_file():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=path))
        return tuple(sources)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "smarrtbot.db"


def load_settings(path: str | Path | None = None) -> Settings:
    if path is not None:
        os.environ["SMARRTBOT_CONFIG"] = str(path)
    return Settings()
