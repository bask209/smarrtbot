"""Tiny HTTP health endpoint (``GET /healthz``) for the Docker healthcheck and dashboards.

Healthy means the event loop is alive (heartbeat job ran recently) and Telegram polling has
started. It never exposes configuration or secrets.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from smarrtbot import __version__


@dataclass
class HealthState:
    started: float = field(default_factory=time.time)
    heartbeat: float = 0.0
    polling: bool = False
    max_age: float = 180.0

    def healthy(self) -> bool:
        return self.polling and (time.time() - self.heartbeat) < self.max_age

    def body(self) -> dict[str, object]:
        return {
            "status": "ok" if self.healthy() else "unhealthy",
            "version": __version__,
            "uptime_s": int(time.time() - self.started),
            "heartbeat_age_s": int(time.time() - self.heartbeat) if self.heartbeat else None,
        }


class HealthServer:
    def __init__(self, state: HealthState, host: str, port: int) -> None:
        self.state = state
        self.host = host
        self.port = port
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = line.decode("latin-1").split()
            path = parts[1] if len(parts) > 1 else "/"
            if path.split("?")[0] in ("/healthz", "/health", "/"):
                ok = self.state.healthy()
                body = json.dumps(self.state.body()).encode()
                status = "200 OK" if ok else "503 Service Unavailable"
            else:
                body, status = b'{"error":"not found"}', "404 Not Found"
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (TimeoutError, ConnectionError, UnicodeDecodeError):
            pass
        finally:
            writer.close()


def probe(url: str = "http://127.0.0.1:8080/healthz", timeout: float = 5.0) -> int:
    """Exit code for ``smarrtbot healthcheck`` (used by the Dockerfile HEALTHCHECK)."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - local only
            return 0 if resp.status == 200 else 1
    except OSError:
        return 1
