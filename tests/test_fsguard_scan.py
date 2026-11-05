"""Path confinement and the idle-IO scanner."""

import os

import pytest

from smarrtbot.config import PathMap
from smarrtbot.fsguard import PathGuard, PathRefused
from smarrtbot.scan import Scanner, scan_roots, set_idle_io_priority


@pytest.fixture
def tree(tmp_path):
    base = tmp_path.resolve()
    media = base / "data/media/movies"
    torrents = base / "data/torrents"
    secret = base / "photos"
    for d in (media, torrents, secret):
        d.mkdir(parents=True)
    (secret / "private.jpg").write_bytes(b"x")
    (torrents / "orphan.bin").write_bytes(b"x" * 10)
    (torrents / "linked.mkv").write_bytes(b"y" * 20)
    os.link(torrents / "linked.mkv", media / "linked.mkv")
    os.symlink(secret / "private.jpg", torrents / "evil.jpg")
    guard = PathGuard([str(media), str(torrents)], [str(torrents)])
    return guard, base


def test_refuses_outside_roots(tree):
    g, base = tree
    with pytest.raises(PathRefused, match="outside"):
        g.check(str(base / "photos/private.jpg"))
    with pytest.raises(PathRefused, match="traversal"):
        g.check(str(base / "data/torrents/../../photos/private.jpg"))
    with pytest.raises(PathRefused):
        g.check("relative/path")


def test_refuses_symlinks_and_roots(tree):
    g, base = tree
    with pytest.raises(PathRefused, match="symlink"):
        g.check(str(base / "data/torrents/evil.jpg"))
    with pytest.raises(PathRefused, match="root"):
        g.check(str(base / "data/torrents"))
    with pytest.raises(PathRefused):
        g.delete_file(str(base / "data/torrents"), dry_run=False)


def test_media_is_never_writable(tree):
    g, base = tree
    with pytest.raises(PathRefused):
        g.delete_file(str(base / "data/media/movies/linked.mkv"), dry_run=False)
    assert (base / "data/media/movies/linked.mkv").exists()


def test_delete_requires_single_link_and_size(tree):
    g, base = tree
    with pytest.raises(PathRefused, match="hardlinks"):
        g.delete_file(str(base / "data/torrents/linked.mkv"), dry_run=False)
    with pytest.raises(PathRefused, match="changed"):
        g.delete_file(str(base / "data/torrents/orphan.bin"), expect_size=99, dry_run=False)
    assert g.delete_file(str(base / "data/torrents/orphan.bin"), dry_run=True) == 0
    assert (base / "data/torrents/orphan.bin").exists()
    assert (
        g.delete_file(str(base / "data/torrents/orphan.bin"), expect_size=10, dry_run=False) == 10
    )
    assert not (base / "data/torrents/orphan.bin").exists()


def test_prunes_empty_dirs_but_not_root(tree):
    g, base = tree
    d = base / "data/torrents/a/b"
    d.mkdir(parents=True)
    (d / "f").write_bytes(b"z")
    g.delete_file(str(d / "f"), dry_run=False)
    assert not (base / "data/torrents/a").exists()
    assert (base / "data/torrents").is_dir()


def test_writable_root_must_be_inside_read_roots(tmp_path):
    with pytest.raises(ValueError):
        PathGuard([str(tmp_path / "a")], [str(tmp_path / "b")])
    with pytest.raises(ValueError):
        PathGuard(["/"], [])


def test_path_maps(tmp_path):
    g = PathGuard([str(tmp_path)], [], [PathMap(src="/data", dst=str(tmp_path))])
    assert g.map_path("/data/media/x.mkv") == f"{tmp_path}/media/x.mkv"
    assert g.map_path("/database/x") == "/database/x"


def test_scan_counts_inodes_once(tree):
    _, base = tree
    res = scan_roots([str(base / "data/media/movies"), str(base / "data/torrents")])
    # orphan(10) + linked(20, counted once); the symlink is ignored.
    assert res.unique_bytes == 30
    troot = res.roots[str(base / "data/torrents")]
    assert troot.apparent_bytes == 30 and troot.single_link_bytes == 10
    assert not any(f.path.endswith("evil.jpg") for f in res.files)


async def test_scanner_cache_and_rate_limit(tree, tmp_path):
    _, base = tree
    cache = tmp_path / "state/scan.json"
    sc = Scanner([str(base / "data/torrents")], cache, min_interval_s=3600)
    r1 = await sc.refresh()
    (base / "data/torrents/new.bin").write_bytes(b"n")
    r2 = await sc.refresh()
    assert r2 is r1  # rate limited: cached
    r3 = await sc.refresh(force=True)
    assert (
        r3.roots[str(base / "data/torrents")].files
        == r1.roots[str(base / "data/torrents")].files + 1
    )
    again = Scanner([str(base / "data/torrents")], cache)
    assert again.result is not None and again.result.unique_bytes == r3.unique_bytes


def test_idle_io_priority_does_not_crash():
    assert set_idle_io_priority() in (True, False)
