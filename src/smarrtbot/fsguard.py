"""Path confinement for every filesystem touch the bot makes.

Defense in depth on top of the container mounts:
* every path is normalised and must equal its ``realpath`` (no symlink anywhere on the way),
* it must sit strictly inside a configured root (roots themselves are never targets),
* deletes are allowed only inside *writable* roots, only for regular files, and the caller can
  require a link count of 1 (so a file whose other hardlink lives in the library is never
  touched).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass

from smarrtbot.config import PathMap


class PathRefused(ValueError):
    """The path is outside the allowed roots, a symlink, a root, or otherwise unsafe."""


@dataclass(frozen=True)
class GuardedFile:
    path: str
    size: int
    inode: int
    dev: int
    nlink: int
    mtime: float


class PathGuard:
    def __init__(
        self,
        read_roots: list[str],
        write_roots: list[str],
        path_maps: list[PathMap] | None = None,
    ) -> None:
        self.read_roots = [self._norm_root(r) for r in read_roots]
        self.write_roots = [self._norm_root(r) for r in write_roots]
        for w in self.write_roots:
            if not any(_within_or_equal(w, r) for r in self.read_roots):
                raise ValueError(f"writable root {w} is not inside a read root")
        self.path_maps = sorted(path_maps or [], key=lambda m: -len(m.src))

    @staticmethod
    def _norm_root(root: str) -> str:
        if not root.startswith("/"):
            raise ValueError(f"root must be absolute: {root}")
        norm = os.path.normpath(root)
        if norm == "/":
            raise ValueError("refusing '/' as a root")
        return norm

    def map_path(self, service_path: str) -> str:
        """Translate a path as Radarr/Sonarr/qBittorrent report it into this container's view."""
        for m in self.path_maps:
            src = m.src.rstrip("/")
            if service_path == src or service_path.startswith(src + "/"):
                return m.dst.rstrip("/") + service_path[len(src) :]
        return service_path

    def check(self, path: str, *, write: bool = False) -> str:
        """Return the normalised path if it is allowed, else raise PathRefused."""
        if not path or not path.startswith("/"):
            raise PathRefused("path must be absolute")
        if "\x00" in path:
            raise PathRefused("NUL in path")
        if any(part == ".." for part in path.split("/")):
            raise PathRefused("traversal ('..') refused")
        norm = os.path.normpath(path)
        roots = self.write_roots if write else self.read_roots
        root = next((r for r in roots if _within(norm, r)), None)
        if root is None:
            if any(norm == r for r in self.read_roots + self.write_roots):
                raise PathRefused("a root folder itself is never a target")
            raise PathRefused("outside the allowed roots")
        real = os.path.realpath(norm)
        if real != norm:
            raise PathRefused("symlinks are refused")
        if not _within(real, root):  # pragma: no cover - implied by the equality above
            raise PathRefused("resolves outside its root")
        return norm

    def stat_file(self, path: str) -> GuardedFile | None:
        """lstat a file inside the read roots; None if it does not exist."""
        norm = self.check(path)
        try:
            st = os.lstat(norm)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(st.st_mode):
            raise PathRefused("not a regular file")
        return GuardedFile(norm, st.st_size, st.st_ino, st.st_dev, st.st_nlink, st.st_mtime)

    def delete_file(
        self,
        path: str,
        *,
        require_single_link: bool = True,
        expect_size: int | None = None,
        dry_run: bool = True,
    ) -> int:
        """Delete one regular file inside a writable root. Returns bytes freed (0 if dry run)."""
        norm = self.check(path, write=True)
        st = os.lstat(norm)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise PathRefused("not a regular file")
        if require_single_link and st.st_nlink != 1:
            raise PathRefused("file has other hardlinks (it is part of the library)")
        if expect_size is not None and st.st_size != expect_size:
            raise PathRefused("file changed since the preview")
        if dry_run:
            return 0
        os.unlink(norm)
        self._prune_empty_dirs(os.path.dirname(norm))
        return st.st_size if st.st_nlink == 1 else 0

    def _prune_empty_dirs(self, directory: str) -> None:
        root = next((r for r in self.write_roots if _within(directory, r)), None)
        while root and _within(directory, root):
            try:
                os.rmdir(directory)  # only succeeds when empty
            except OSError:
                return
            directory = os.path.dirname(directory)


def _within(path: str, root: str) -> bool:
    """True if path is strictly inside root."""
    return path != root and path.startswith(root.rstrip("/") + "/")


def _within_or_equal(path: str, root: str) -> bool:
    return path == root or _within(path, root)
