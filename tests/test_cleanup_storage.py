"""Cleanup candidates, guarded execution, storage report and alerts."""

import os
import time

import pytest

from smarrtbot.flows import cleanup as c
from smarrtbot.flows import storage as st
from tests.fakes import ALICE, BOB, GROUP, MB, add_movie, make_services


@pytest.fixture
def env(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11")  # hardlinked into the library
    # A completed torrent that was never imported (no hardlink into the library).
    env.write("/data/torrents/radarr/Loose.Pack/loose.mkv", 5 * MB)
    svc.qbit.add("cc33", "Loose.Pack", "/data/torrents/radarr", ["Loose.Pack/loose.mkv"])
    # Unmanaged leftovers.
    env.write("/data/torrents/radarr/old-leftover.mkv", 2 * MB)
    env.write("/data/torrents/radarr/Show.S01E02.rar", MB)
    env.write("/data/torrents/radarr/setup.exe", MB)
    env.write("/data/torrents/incomplete/partial.mkv", MB)
    return svc, env


async def test_candidates_by_category(env):
    svc, _ = env
    items = await c.list_candidates(svc)
    by = {}
    for x in items:
        by.setdefault(x.category, []).append(x)
    orphan_labels = {x.label for x in by["orphans"]}
    assert "radarr/old-leftover.mkv" in orphan_labels
    assert not any("incomplete" in lbl for lbl in orphan_labels)  # ignored dir
    assert not any("Alpha" in lbl for lbl in orphan_labels)  # nlink 2 -> never orphan
    assert not any("loose.mkv" in lbl for lbl in orphan_labels)  # owned by a torrent
    assert [x.ref for x in by["unlinked_torrents"]] == ["cc33"]
    assert {x.label for x in by["suspicious"]} == {"Show.S01E02.rar", "setup.exe"}
    assert by["largest"][0].action == "title_delete"


async def test_cleanup_proposal_needs_known_ids(env):
    svc, _ = env
    with pytest.raises(c.CleanupError, match="unknown"):
        await c.propose_cleanup(svc, ["deadbeef00"], ALICE, GROUP)
    items = await c.list_candidates(svc)
    largest = next(x for x in items if x.category == "largest")
    with pytest.raises(c.CleanupError, match="delete_title"):
        await c.propose_cleanup(svc, [largest.id], ALICE, GROUP)


async def test_cleanup_executes_with_revalidation(env):
    svc, e = env
    items = await c.list_candidates(svc)
    orphan = next(x for x in items if x.label == "radarr/old-leftover.mkv")
    exe = next(x for x in items if x.label == "setup.exe")
    torrent = next(x for x in items if x.category == "unlinked_torrents")
    p = await c.propose_cleanup(svc, [orphan.id, exe.id, torrent.id], ALICE, GROUP)
    assert p.size == 2 * MB + MB + 5 * MB
    # Between preview and confirm, the leftover gains a hardlink: it must be skipped.
    e.link("/data/torrents/radarr/setup.exe", "/data/media/movies/x/setup.exe")
    action = svc.pending.claim(p.nonce, ALICE).action
    res = await c.execute_cleanup(svc, action.payload, ALICE, dry_run=False)
    assert ("setup.exe", "file has other hardlinks (it is part of the library)") in res.skipped
    assert not os.path.exists(e.local("/data/torrents/radarr/old-leftover.mkv"))
    assert svc.qbit.calls == [("delete", ["cc33"])]
    assert res.freed == 2 * MB + 5 * MB
    assert svc.db.recent_audit(1)[0]["action"] == "cleanup"


async def test_cleanup_dry_run_deletes_nothing(env):
    svc, e = env
    items = await c.list_candidates(svc, "orphans")
    p = await c.propose_cleanup(svc, [i.id for i in items], BOB, GROUP)
    action = svc.pending.claim(p.nonce, BOB).action
    res = await c.execute_cleanup(svc, action.payload, BOB, dry_run=True)
    assert res.freed == 0 and res.deleted
    assert os.path.exists(e.local("/data/torrents/radarr/old-leftover.mkv"))


async def test_orphan_min_age(tmp_path):
    svc, e = make_services(tmp_path, storage={"orphan_min_age_minutes": 60})
    e.write("/data/torrents/radarr/fresh.mkv", MB)
    assert [x for x in await c.list_candidates(svc, "orphans")] == []
    old = time.time() - 7200
    os.utime(e.local("/data/torrents/radarr/fresh.mkv"), (old, old))
    await svc.scanner.refresh(force=True)
    assert len(await c.list_candidates(svc, "orphans")) == 1


async def test_storage_report_and_alert_once(env):
    svc, _ = env
    await svc.scanner.refresh(force=True)
    rep = await st.storage_report(svc)
    assert rep.total and rep.free is not None
    assert rep.movies == 3 * MB
    assert rep.torrents == 3 * MB + 5 * MB
    assert rep.torrents_unique is not None and rep.torrents_unique >= 5 * MB
    tool = rep.as_tool()
    assert tool["largest_titles"][0]["title"] == "Alpha"
    # Thresholds: force "low" with an absurd GB threshold, alert exactly once.
    svc.settings.storage.alert_free_gb = 10**9
    assert await st.check_alert(svc) is not None
    assert await st.check_alert(svc) is None
    svc.settings.storage.alert_free_gb = 0
    svc.settings.storage.alert_free_percent = 0
    assert await st.check_alert(svc) is None
    assert svc.db.kv_get("storage_alert_active") is False


async def test_snapshot_deltas(env):
    svc, _ = env
    import datetime as dt

    rep = await st.take_snapshot(svc)
    old_day = (dt.date.today() - dt.timedelta(days=8)).isoformat()
    svc.db.put_snapshot(old_day, total=rep.total, free=rep.free + 10 * MB)
    rep2 = await st.storage_report(svc)
    assert rep2.delta_7d == 10 * MB
