"""Hardlink-aware delete flow."""

import pytest

from smarrtbot.flows import delete as d
from tests.fakes import ALICE, BOB, GROUP, MB, OWNER, add_movie, make_services


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    async def nosleep(_s):
        return None

    monkeypatch.setattr(d.asyncio, "sleep", nosleep)


async def test_movie_plan_finds_torrent_and_expected_freed(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11", extra_torrent_files={"sample.mkv": MB})
    plan = await d.plan_delete(svc, "movie", 1)
    assert plan.hardlink_check
    assert [t.hash for t in plan.deletable_torrents] == ["aa11"]
    # 3 MB media (hardlinked to the torrent copy) + 1 MB sample: 4 MB unique bytes freed.
    assert plan.expected_freed == 4 * MB
    assert plan.media_bytes == 3 * MB


async def test_order_arr_first_then_torrent_and_space_measured(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11")
    p, _ = await d.propose_delete(svc, "movie", 1, None, ALICE, GROUP)
    action = svc.pending.claim(p.nonce, ALICE).action
    res = await d.execute_delete(svc, action.payload, ALICE, dry_run=False)
    assert svc.radarr.calls[0] == ("delete_movie", 1)
    assert svc.qbit.calls == [("delete", ["aa11"])]
    assert res.measured_freed == 3 * MB
    assert res.expected_freed == 3 * MB
    assert not res.mismatch
    assert res.torrents_seeding == ["Alpha.2001.1080p"]  # stalledUP counts as seeding
    row = svc.db.recent_audit(1)[0]
    assert row["action"] == "delete" and row["user_id"] == ALICE and row["bytes_freed"] == 3 * MB


async def test_no_torrent_found_never_guesses(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", None)
    # A torrent for a different title exists; it must not be picked.
    add_movie(svc, env, 2, "Beta", "bb22")
    plan = await d.plan_delete(svc, "movie", 1)
    assert plan.torrents == []
    assert "no torrent found" in plan.notes
    # Media file is still hardlinked (we created it via link) -> nothing freed.
    assert plan.expected_freed == 0


async def test_shared_torrent_by_history_is_kept(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11")
    # Another movie was imported from the same torrent (e.g. a collection pack).
    svc.radarr.history.append(
        {"id": 999, "eventType": "downloadFolderImported", "movieId": 7, "downloadId": "AA11"}
    )
    plan = await d.plan_delete(svc, "movie", 1)
    assert plan.deletable_torrents == []
    assert plan.kept_torrents[0].reasons
    assert plan.expected_freed == 0  # media link alone frees nothing while the torrent stays


async def test_shared_torrent_by_extra_hardlink_is_kept(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11")
    # Someone hardlinked the torrent file somewhere else in the library.
    env.link("/data/torrents/radarr/Alpha.2001.1080p/Alpha.mkv", "/data/media/movies/Copy/a.mkv")
    plan = await d.plan_delete(svc, "movie", 1)
    assert plan.deletable_torrents == []
    assert "hardlinked outside" in plan.kept_torrents[0].reasons[0]


def _season_pack(svc, env, seasons=(1, 2), pack_hash="ss99"):
    """A series with a multi-season pack torrent, hardlinked into the library."""
    svc.sonarr.series_by_id[5] = {
        "id": 5, "title": "Gamma", "year": 2010, "path": "/data/media/tv/Gamma",
        "seasons": [{"seasonNumber": s, "monitored": True} for s in seasons],
        "statistics": {"sizeOnDisk": 0, "episodeFileCount": len(seasons)},
    }  # fmt: skip
    files = []
    fid = 50
    for s in seasons:
        tp = f"/data/torrents/tv-sonarr/Gamma.Pack/S{s:02d}E01.mkv"
        mp = f"/data/media/tv/Gamma/Season {s}/Gamma S{s:02d}E01.mkv"
        env.write(tp, 2 * MB)
        env.link(tp, mp)
        fid += 1
        svc.sonarr.files.append(
            {"id": fid, "seriesId": 5, "seasonNumber": s, "path": mp, "size": 2 * MB}
        )
        svc.sonarr.episodes_list.append({"id": 500 + s, "seriesId": 5, "seasonNumber": s})
        svc.sonarr.history.append(
            {"id": 900 + s, "eventType": "downloadFolderImported", "seriesId": 5,
             "episodeId": 500 + s, "downloadId": pack_hash.upper(), "episode": {"seasonNumber": s}}
        )  # fmt: skip
        files.append(f"Gamma.Pack/S{s:02d}E01.mkv")
    svc.qbit.add(pack_hash, "Gamma.Pack", "/data/torrents/tv-sonarr", files)


async def test_season_delete_keeps_multi_season_pack(tmp_path):
    svc, env = make_services(tmp_path)
    _season_pack(svc, env)
    p, plan = await d.propose_delete(svc, "season", 5, 1, ALICE, GROUP)
    assert plan.deletable_torrents == []
    assert len(plan.kept_torrents) == 1
    assert "torrent_kept:" in " ".join(p.details)
    action = svc.pending.claim(p.nonce, ALICE).action
    res = await d.execute_delete(svc, action.payload, ALICE, dry_run=False)
    assert ("delete_episode_files", [51]) in svc.sonarr.calls
    assert ("set_episodes_monitored", ([501], False)) in svc.sonarr.calls
    assert svc.sonarr.series_by_id[5]["seasons"][0]["monitored"] is False
    assert svc.qbit.calls == []  # pack kept
    assert res.torrents_kept and res.measured_freed == 0


async def test_whole_series_delete_removes_pack(tmp_path):
    svc, env = make_services(tmp_path)
    _season_pack(svc, env)
    p, plan = await d.propose_delete(svc, "series", 5, None, ALICE, GROUP)
    assert [t.hash for t in plan.deletable_torrents] == ["ss99"]
    assert plan.expected_freed == 4 * MB
    action = svc.pending.claim(p.nonce, ALICE).action
    res = await d.execute_delete(svc, action.payload, ALICE, dry_run=False)
    assert svc.sonarr.calls[0] == ("delete_series", 5)
    assert svc.qbit.calls == [("delete", ["ss99"])]
    assert res.measured_freed == 4 * MB


async def test_dry_run_changes_nothing(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11")
    p, _ = await d.propose_delete(svc, "movie", 1, None, ALICE, GROUP)
    action = svc.pending.claim(p.nonce, ALICE).action
    res = await d.execute_delete(svc, action.payload, ALICE, dry_run=True)
    assert res.dry_run
    assert svc.radarr.calls == [] and svc.qbit.calls == []
    assert 1 in svc.radarr.movies_by_id


async def test_drift_between_confirm_and_execute_aborts(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11")
    p, _ = await d.propose_delete(svc, "movie", 1, None, ALICE, GROUP)
    svc.radarr.movies_by_id[1]["movieFile"]["id"] = 4242  # upgraded meanwhile
    action = svc.pending.claim(p.nonce, ALICE).action
    with pytest.raises(d.DeleteError, match="changed"):
        await d.execute_delete(svc, action.payload, ALICE, dry_run=False)
    assert svc.radarr.calls == []


async def test_rate_limit_per_user_and_owner_exempt(tmp_path):
    svc, env = make_services(tmp_path, limits={"deletes_per_hour": 2})
    for _ in range(2):
        svc.db.audit(BOB, "delete", "x", 0, False)
        svc.db.audit(OWNER, "delete", "x", 0, False)
    add_movie(svc, env, 1, "Alpha", "aa11")
    with pytest.raises(d.DeleteError, match="rate limit"):
        await d.propose_delete(svc, "movie", 1, None, BOB, GROUP)
    p, _ = await d.propose_delete(svc, "movie", 1, None, OWNER, GROUP)
    assert p.nonce
    # Dry-run audit rows do not count toward the limit.
    svc.db.audit(ALICE, "delete", "x", 0, True)
    svc.db.audit(ALICE, "delete", "x", 0, True)
    assert (await d.propose_delete(svc, "movie", 1, None, ALICE, GROUP))[0].nonce


async def test_daily_cap(tmp_path):
    svc, env = make_services(tmp_path, limits={"daily_delete_cap_gb": 0.001})  # ~1 MB
    add_movie(svc, env, 1, "Alpha", "aa11")
    with pytest.raises(d.DeleteError, match="daily cap"):
        await d.propose_delete(svc, "movie", 1, None, OWNER, GROUP)


async def test_missing_mount_disables_hardlink_check(tmp_path):
    svc, env = make_services(tmp_path)
    add_movie(svc, env, 1, "Alpha", "aa11")
    svc.guard.path_maps = []  # service paths no longer resolve inside the roots
    plan = await d.plan_delete(svc, "movie", 1)
    assert not plan.hardlink_check
    assert plan.expected_freed is None
    assert "no_hardlink_check" in d.plan_details(plan)
