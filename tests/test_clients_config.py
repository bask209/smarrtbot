"""HTTP clients (respx), config loading, health endpoint, i18n and the auth gate."""

import asyncio

import httpx
import pytest
import respx

from smarrtbot.bot import auth
from smarrtbot.bot.i18n import STRINGS, detect_lang, render_details, t
from smarrtbot.clients.arr import RadarrClient, SonarrClient
from smarrtbot.clients.http import ServiceError, ServiceUnavailable
from smarrtbot.clients.qbit import QbitClient
from smarrtbot.config import SecretError, Settings, load_settings, read_secret
from smarrtbot.db import Database
from smarrtbot.health import HealthServer, HealthState, probe

R = "http://arr-host:7878"
Q = "http://arr-host:8085"


async def nosleep(_):
    return None


@respx.mock
async def test_arr_retries_connect_errors_then_succeeds():
    route = respx.get(f"{R}/api/v3/system/status").mock(
        side_effect=[httpx.ConnectError("down"), httpx.Response(200, json={"version": "6"})]
    )
    c = RadarrClient(R, "k" * 32, sleep=nosleep)
    assert (await c.system_status())["version"] == "6"
    assert route.call_count == 2
    assert route.calls.last.request.headers["X-Api-Key"] == "k" * 32


@respx.mock
async def test_arr_gives_up_and_does_not_retry_post_after_read_timeout():
    respx.get(f"{R}/api/v3/system/status").mock(side_effect=httpx.ConnectError("down"))
    c = RadarrClient(R, "k", retries=2, sleep=nosleep)
    with pytest.raises(ServiceUnavailable):
        await c.system_status()
    post = respx.post(f"{R}/api/v3/movie").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ServiceUnavailable):
        await c.add_movie({"title": "x", "tmdbId": 1}, 1, "/m")
    assert post.call_count == 1


@respx.mock
async def test_arr_4xx_is_a_service_error_with_message():
    respx.get(f"{R}/api/v3/movie/9").mock(
        return_value=httpx.Response(404, json={"message": "NotFound"})
    )
    with pytest.raises(ServiceError, match="NotFound"):
        await RadarrClient(R, "k").movie(9)


@respx.mock
async def test_history_by_download_uppercases_hash():
    route = respx.get(f"{R}/api/v3/history").mock(
        return_value=httpx.Response(200, json={"records": [{"id": 1}]})
    )
    recs = await RadarrClient(R, "k").history_by_download("abcdef")
    assert recs == [{"id": 1}]
    assert route.calls.last.request.url.params["downloadId"] == "ABCDEF"


@respx.mock
async def test_sonarr_lookup_tvdb_and_free_space():
    respx.get(f"{R}/api/v3/series/lookup").mock(
        return_value=httpx.Response(200, json=[{"tvdbId": 5, "title": "x"}, {"tvdbId": 6}])
    )
    respx.get(f"{R}/api/v3/diskspace").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"path": "/", "freeSpace": 1, "totalSpace": 9},
                {"path": "/data", "freeSpace": 42, "totalSpace": 99},
            ],
        )
    )
    s = SonarrClient(R, "k")
    assert (await s.lookup_tvdb(5))["title"] == "x"
    assert await s.free_space_for("/data/media/tv/Show") == 42
    assert await s.free_space_for("/config") == 1


@respx.mock
async def test_qbit_login_and_relogin_on_403():
    login = respx.post(f"{Q}/api/v2/auth/login").mock(
        return_value=httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=abc; path=/"})
    )
    info = respx.get(f"{Q}/api/v2/torrents/info").mock(
        side_effect=[
            httpx.Response(403),
            httpx.Response(200, json=[{"hash": "aa"}]),
        ]
    )
    delete = respx.post(f"{Q}/api/v2/torrents/delete").mock(return_value=httpx.Response(200))
    q = QbitClient(Q, "admin", "pw", sleep=nosleep)
    assert await q.torrents(["AA"]) == [{"hash": "aa"}]
    assert login.call_count == 2 and info.call_count == 2
    assert info.calls.last.request.url.params["hashes"] == "aa"
    await q.delete(["AA", "bb"])
    body = delete.calls.last.request.content.decode()
    assert "hashes=aa%7Cbb" in body and "deleteFiles=true" in body


@respx.mock
async def test_qbit_bad_login():
    respx.post(f"{Q}/api/v2/auth/login").mock(return_value=httpx.Response(200, text="Fails."))
    with pytest.raises(ServiceError, match="login failed"):
        await QbitClient(Q, "admin", "pw").version()


def test_read_secret(tmp_path):
    p = tmp_path / "s"
    p.write_text("  value\n")
    assert read_secret(p) == "value"
    p.write_text("")
    with pytest.raises(SecretError, match="empty"):
        read_secret(p)
    with pytest.raises(SecretError) as exc:
        read_secret(tmp_path / "missing", name="x")
    assert "value" not in str(exc.value)


def test_settings_yaml_and_env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "dry_run: false\ntelegram:\n  owner_id: 42\n  allowed_group_ids: [-1001]\n"
        "radarr:\n  url: http://arr-host:7878\n"
    )
    monkeypatch.setenv("SMARRTBOT_TELEGRAM__OWNER_ID", "43")
    s = load_settings(cfg)
    assert s.dry_run is False
    assert s.telegram.owner_id == 43  # env wins
    assert s.telegram.allowed_group_ids == [-1001]
    assert s.radarr.enabled and not s.sonarr.enabled
    monkeypatch.delenv("SMARRTBOT_CONFIG")
    assert Settings().dry_run is True  # safe default


def test_settings_reject_relative_roots():
    with pytest.raises(ValueError):
        Settings(storage={"movies_root": "media/movies"})


async def test_health_endpoint():
    state = HealthState()
    server = HealthServer(state, "127.0.0.1", 0)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/healthz"
    assert await asyncio.to_thread(probe, url) == 1  # not polling yet
    state.polling, state.heartbeat = True, __import__("time").time()
    assert await asyncio.to_thread(probe, url) == 0
    await server.stop()


def test_detect_lang():
    assert detect_lang("¿cuánto espacio queda?") == "es"
    assert detect_lang("agrega la pelicula Dune") == "es"
    assert detect_lang("borra la temporada 2 de la serie") == "es"
    assert detect_lang("how much space is left?") == "en"
    assert detect_lang("add Dune please") == "en"
    assert detect_lang("Dune", fallback="es") == "es"


def test_i18n_keys_match_and_render():
    assert set(STRINGS["en"]) == set(STRINGS["es"])
    assert "Privacy" in t("en", "help", dry="")
    assert "Privacidad" in t("es", "help", dry="")
    txt = render_details("es", ["media:1:1073741824", "torrent_delete:5:1", "no_torrent"])
    assert "1.0 GB" in txt and "compartiendo" in txt
    assert t("xx", "cancelled") == "Cancelled."


def test_auth_gate():
    s = Settings(telegram={"owner_id": 1, "allowed_group_ids": [-5]})
    db = Database(":memory:")
    db.set_user_status(2, "approved", 1)
    db.set_user_status(3, "revoked", 1)
    assert auth.gate(s, db, 1, "private", 1).allowed
    assert auth.gate(s, db, -5, "supergroup", 2).allowed
    assert not auth.gate(s, db, -6, "supergroup", 2).allowed  # group not allowlisted
    assert not auth.gate(s, db, -5, "supergroup", 3).allowed  # revoked
    assert not auth.gate(s, db, 4, "private", 4).allowed  # unknown user
    assert not auth.gate(s, db, -5, "channel", 1).allowed
    unset = Settings()
    assert not auth.gate(unset, db, 1, "private", 1).allowed  # owner must be configured


def test_example_config_and_stack_are_valid(monkeypatch):
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parents[1]
    s = load_settings(root / "examples/config.example.yaml")
    monkeypatch.delenv("SMARRTBOT_CONFIG")
    assert s.dry_run is True and s.mediaserver.kind == "plex"
    assert s.storage.writable_roots == ["/data/torrents"]
    stack = yaml.safe_load((root / "examples/stack.example.yml").read_text())
    svc = stack["services"]["smarrtbot"]
    assert ":latest" not in svc["image"] and svc["mem_limit"] == "256m"
    assert any(v.endswith("/data/media:ro") for v in svc["volumes"])
