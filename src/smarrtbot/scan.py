"""Cached, rate-limited filesystem scan of the allowed roots at idle IO priority.

The scan runs in its own thread, which lowers its CPU niceness and IO priority (``ioprio_set``
syscall, IDLE class) so it yields to everything else on a fragile disk. It counts each inode once
so "unique bytes" add up even though the library is hardlinked into the torrents folder.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import platform
import stat
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_IOPRIO_SYSCALL = {"x86_64": 251, "aarch64": 30, "armv7l": 314, "armv6l": 314}
_IOPRIO_WHO_PROCESS = 1
_IOPRIO_CLASS_IDLE = 3
_IOPRIO_CLASS_SHIFT = 13


def set_idle_io_priority() -> bool:
    """Put the *calling thread* in the IDLE IO class. Returns False if unsupported."""
    nr = _IOPRIO_SYSCALL.get(platform.machine())
    if nr is None:
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        ok = libc.syscall(nr, _IOPRIO_WHO_PROCESS, 0, _IOPRIO_CLASS_IDLE << _IOPRIO_CLASS_SHIFT)
        return ok == 0
    except (OSError, AttributeError):
        return False


@dataclass
class ScanFile:
    path: str
    size: int
    inode: int
    nlink: int
    mtime: float


@dataclass
class RootStats:
    files: int = 0
    apparent_bytes: int = 0
    # bytes of files whose only link is here (nlink == 1)
    single_link_bytes: int = 0


@dataclass
class ScanResult:
    ts: float
    duration: float
    roots: dict[str, RootStats] = field(default_factory=dict)
    unique_bytes: int = 0
    files: list[ScanFile] = field(default_factory=list)
    errors: int = 0

    def by_root(self, root: str) -> list[ScanFile]:
        prefix = root.rstrip("/") + "/"
        return [f for f in self.files if f.path.startswith(prefix)]


def scan_roots(
    roots: list[str],
    *,
    ignore_dirs: list[str] | None = None,
    pause_every: int = 500,
    pause_s: float = 0.02,
) -> ScanResult:
    t0 = time.time()
    ignore = set(ignore_dirs or [])
    result = ScanResult(ts=t0, duration=0.0)
    seen: set[tuple[int, int]] = set()
    count = 0
    for root in roots:
        rs = RootStats()
        result.roots[root] = rs
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                result.errors += 1
                continue
            with it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        result.errors += 1
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        if entry.name not in ignore:
                            stack.append(entry.path)
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue  # symlinks, sockets, ... are never counted or touched
                    rs.files += 1
                    rs.apparent_bytes += st.st_size
                    if st.st_nlink == 1:
                        rs.single_link_bytes += st.st_size
                    key = (st.st_dev, st.st_ino)
                    if key not in seen:
                        seen.add(key)
                        result.unique_bytes += st.st_size
                    result.files.append(
                        ScanFile(entry.path, st.st_size, st.st_ino, st.st_nlink, st.st_mtime)
                    )
                    count += 1
                    if pause_every and count % pause_every == 0:
                        time.sleep(pause_s)
    result.duration = time.time() - t0
    return result


class Scanner:
    def __init__(
        self,
        roots: list[str],
        cache_path: Path,
        *,
        min_interval_s: float = 3600,
        ignore_dirs: list[str] | None = None,
    ) -> None:
        self.roots = roots
        self.cache_path = cache_path
        self.min_interval_s = min_interval_s
        self.ignore_dirs = ignore_dirs or []
        self._result: ScanResult | None = None
        self._lock = asyncio.Lock()
        self._load_cache()

    @property
    def result(self) -> ScanResult | None:
        return self._result

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return
        try:
            self._result = ScanResult(
                ts=raw["ts"],
                duration=raw["duration"],
                roots={k: RootStats(**v) for k, v in raw["roots"].items()},
                unique_bytes=raw["unique_bytes"],
                files=[ScanFile(**f) for f in raw["files"]],
                errors=raw.get("errors", 0),
            )
        except (KeyError, TypeError):
            self._result = None

    def _save_cache(self, res: ScanResult) -> None:
        tmp = self.cache_path.with_suffix(".tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(asdict(res)))
            tmp.replace(self.cache_path)
        except OSError as exc:
            log.warning("scan cache not saved: %s", exc.strerror)

    def age(self) -> float | None:
        return None if self._result is None else time.time() - self._result.ts

    async def refresh(self, *, force: bool = False) -> ScanResult | None:
        """Run a scan unless one ran recently. Returns the (possibly cached) result."""
        async with self._lock:
            age = self.age()
            if not force and age is not None and age < self.min_interval_s:
                return self._result
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[ScanResult] = loop.create_future()

            def worker() -> None:
                set_idle_io_priority()
                try:
                    os.nice(19)
                except OSError:  # pragma: no cover
                    pass
                try:
                    res = scan_roots(self.roots, ignore_dirs=self.ignore_dirs)
                except Exception as exc:  # pragma: no cover - surfaced to the awaiting task
                    loop.call_soon_threadsafe(fut.set_exception, exc)
                else:
                    loop.call_soon_threadsafe(fut.set_result, res)

            threading.Thread(target=worker, name="smarrtbot-scan", daemon=True).start()
            res = await fut
            self._result = res
            self._save_cache(res)
            log.info(
                "scan done: %d files, %.1fs, %d errors", len(res.files), res.duration, res.errors
            )
            return res
