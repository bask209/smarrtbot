"""qBittorrent Web API v2 client (cookie session, re-login on 403)."""

from __future__ import annotations

from typing import Any

from smarrtbot.clients.http import HttpService, ServiceError

SEEDING_STATES = {"uploading", "stalledUP", "forcedUP", "queuedUP", "checkingUP"}


class QbitClient(HttpService):
    name = "qbittorrent"

    def __init__(self, base_url: str, username: str, password: str, **kw: Any) -> None:
        super().__init__(base_url, **kw)
        self._username = username
        self._password = password
        self._logged_in = False

    async def login(self) -> None:
        resp = await self._send(
            "POST",
            "/api/v2/auth/login",
            data={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200 or resp.text.strip() not in ("Ok.", ""):
            raise ServiceError(
                self.name, "login failed (check username/password)", resp.status_code
            )
        self._logged_in = True

    async def call(self, method: str, path: str, **kw: Any) -> Any:
        if not self._logged_in:
            await self.login()
        try:
            return await self.request(method, path, **kw)
        except ServiceError as exc:
            if exc.status not in (401, 403):
                raise
            self._logged_in = False
            await self.login()
            return await self.request(method, path, **kw)

    async def version(self) -> str:
        return str(await self.call("GET", "/api/v2/app/version"))

    async def torrents(self, hashes: list[str] | None = None) -> list[dict[str, Any]]:
        params = {"hashes": "|".join(h.lower() for h in hashes)} if hashes else None
        return await self.call("GET", "/api/v2/torrents/info", params=params) or []

    async def torrent(self, torrent_hash: str) -> dict[str, Any] | None:
        res = await self.torrents([torrent_hash])
        return res[0] if res else None

    async def files(self, torrent_hash: str) -> list[dict[str, Any]]:
        return (
            await self.call("GET", "/api/v2/torrents/files", params={"hash": torrent_hash.lower()})
            or []
        )

    async def delete(self, hashes: list[str], delete_files: bool = True) -> None:
        if not hashes:
            return
        await self.call(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": "|".join(h.lower() for h in hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
        )

    async def transfer_info(self) -> dict[str, Any]:
        return await self.call("GET", "/api/v2/transfer/info") or {}
