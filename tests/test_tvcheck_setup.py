"""TV-friendly check, Plex stream parsing, setup-arr and setup-notify."""

import json
import time

from smarrtbot.flows import tvcheck
from smarrtbot.mediaserver.base import StreamReport
from smarrtbot.mediaserver.plex import parse_streams
from smarrtbot.setup_arr import PROFILE_4K, PROFILE_1080, plan_and_apply
from smarrtbot.setup_notify import plan_and_apply as notify_plan
from tests.fakes import ALICE, add_movie, make_services


def test_evaluate_flags():
    rep = StreamReport(
        file="x.mkv",
        height=2160,
        bitrate_kbps=60000,
        dolby_vision=True,
        audio_codecs=["truehd", "dca:ma", "eac3"],
        embedded_subtitle_codecs=["pgs", "ass"],
    )
    assert tvcheck.evaluate(rep, 25, 12) == [
        "dolby_vision",
        "truehd",
        "dts",
        "pgs_subs",
        "ass_subs",
        "bitrate",
    ]
    ok = StreamReport(file="y.mkv", height=1080, bitrate_kbps=8000, audio_codecs=["eac3"])
    assert tvcheck.evaluate(ok, 25, 12) == []
    assert tvcheck.evaluate(StreamReport(file="z", height=1080, bitrate_kbps=13000), 25, 12) == [
        "bitrate"
    ]


def test_plex_parse_streams_embedded_vs_sidecar():
    meta = {
        "Media": [
            {
                "bitrate": 30000,
                "height": 2160,
                "Part": [
                    {
                        "file": "/media/movies/Film (2020)/Film (2020).mkv",
                        "Stream": [
                            {"streamType": 1, "codec": "hevc", "DOVIPresent": True, "colorTrc": "smpte2084"},
                            {"streamType": 2, "codec": "dca", "profile": "ma"},
                            {"streamType": 3, "codec": "pgs", "index": 3},
                            {"streamType": 3, "codec": "srt", "key": "/library/streams/99"},
                        ],
                    }
                ],
            }
        ]
    }  # fmt: skip
    rep = parse_streams(meta, "Film (2020).mkv")
    assert rep.dolby_vision and rep.hdr == "HDR10"
    assert rep.audio_codecs == ["dca:ma"]
    assert rep.embedded_subtitle_codecs == ["pgs"]  # the Bazarr SRT is a sidecar
    assert parse_streams(meta, "Other.mkv") is None
    meta["Media"][0]["Part"][0]["Stream"] = []
    assert parse_streams(meta, "Film (2020).mkv") is None  # not analysed yet


async def test_poll_sets_cursor_then_queues_new_imports(tmp_path):
    svc, env = make_services(tmp_path)
    svc.sonarr = None
    assert await tvcheck.poll_imports(svc) == 0  # first run: cursor = now, nothing flagged
    add_movie(svc, env, 1, "Alpha", "aa11")
    assert await tvcheck.poll_imports(svc) == 1
    assert await tvcheck.poll_imports(svc) == 0  # cursor moved past it
    row = svc.db.one("SELECT * FROM flags")
    assert row["status"] == "pending" and row["download_id"] == "aa11"


async def test_inspect_backoff_then_flag_and_replace(tmp_path):
    svc, env = make_services(tmp_path)
    svc.sonarr = None
    await tvcheck.poll_imports(svc)
    add_movie(svc, env, 1, "Alpha", "aa11")
    await tvcheck.poll_imports(svc)
    svc.db.execute("UPDATE flags SET next_at=0")
    assert await tvcheck.inspect_due(svc) == []  # Plex has not scanned yet
    row = svc.db.one("SELECT * FROM flags")
    assert row["attempts"] == 1 and row["next_at"] > time.time()
    svc.mediaserver.reports["Alpha (2001).mkv"] = StreamReport(
        file="Alpha (2001).mkv", height=1080, bitrate_kbps=5000, audio_codecs=["truehd"]
    )
    svc.db.execute("UPDATE flags SET next_at=0")
    flagged = await tvcheck.inspect_due(svc)
    assert len(flagged) == 1
    row = svc.db.flag(flagged[0])
    assert json.loads(row["issues"])["issues"] == ["truehd"]
    assert row["title"] == "Alpha"
    msg = await tvcheck.execute_replace(svc, row["id"], ALICE, dry_run=True)
    assert "DRY RUN" in msg and svc.radarr.calls == []
    await tvcheck.execute_replace(svc, row["id"], ALICE, dry_run=False)
    assert ("mark_failed", 100) in svc.radarr.calls
    assert ("delete_movie_file", 10) in svc.radarr.calls
    assert ("search_movies", [1]) in svc.radarr.calls
    assert svc.qbit.calls == [("delete", ["aa11"])]
    assert svc.db.flag(row["id"])["status"] == "replaced"


async def test_inspect_gives_up(tmp_path):
    svc, env = make_services(tmp_path, tvcheck={"max_attempts": 2})
    svc.sonarr = None
    await tvcheck.poll_imports(svc)
    add_movie(svc, env, 1, "Alpha", "aa11")
    await tvcheck.poll_imports(svc)
    for _ in range(2):
        svc.db.execute("UPDATE flags SET next_at=0")
        await tvcheck.inspect_due(svc)
    assert svc.db.one("SELECT status FROM flags")["status"] == "gave_up"


async def test_setup_arr_dry_run_apply_idempotent(tmp_path):
    svc, _ = make_services(tmp_path)
    clients = {"radarr": svc.radarr, "sonarr": svc.sonarr}
    dry = await plan_and_apply(clients, apply=False)
    assert dry.pending() and all(c.action == "create" for c in dry.pending())
    assert svc.radarr.calls == [] and svc.sonarr.calls == []
    await plan_and_apply(clients, apply=True)
    names = {p["name"] for p in svc.radarr.profiles}
    assert {PROFILE_1080, PROFILE_4K} <= names
    p1080 = next(p for p in svc.radarr.profiles if p["name"] == PROFILE_1080)
    allowed = {i.get("name") or i["quality"]["name"] for i in p1080["items"] if i["allowed"]}
    assert "Remux-1080p" not in allowed and "WEB 1080p" in allowed
    dv = next(f for f in p1080["formatItems"] if f["name"] == "SB: Dolby Vision")
    assert dv["score"] == -10000 and p1080["minFormatScore"] == 0
    again = await plan_and_apply(clients, apply=False)
    assert again.pending() == [], again.render()
    # Existing profiles (and titles) are left alone.
    assert any(p["name"] == "HD-1080p" for p in svc.radarr.profiles)


async def test_setup_notify(tmp_path):
    svc, _ = make_services(tmp_path)
    clients = {"radarr": svc.radarr}
    token = "123456789:" + "A" * 35  # fake, built at runtime
    lines = await notify_plan(clients, token, -100, apply=False)
    assert "create" in lines[0] and token not in "\n".join(lines)
    assert svc.radarr.calls == []
    await notify_plan(clients, token, -100, apply=True)
    assert [c[0] for c in svc.radarr.calls] == ["create_notification", "test_notification"]
    n = svc.radarr.notifications_list[0]
    assert n["onDownload"] and n["onGrab"]
    lines = await notify_plan(clients, token, -100, apply=False)
    assert "unchanged" in lines[0]
