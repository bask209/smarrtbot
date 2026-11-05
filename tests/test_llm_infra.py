"""Gemini client error mapping, KeyPool and ModelManager."""

import datetime as dt
import json
import logging
import os

import httpx
import pytest
import respx

from smarrtbot.db import Database
from smarrtbot.llm import gemini as g
from smarrtbot.llm.keypool import KeyPool, seconds_until_pacific_midnight
from smarrtbot.llm.modelmanager import ModelManager, is_candidate, rank
from smarrtbot.logsafe import RedactingFilter, redact

BASE = "https://gemini.test"
KEY_A = "AIza" + "FAKE" + "a" * 31  # built at runtime: no secret-shaped literal
KEY_B = "AIza" + "FAKE" + "b" * 31


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t


def keyfile(tmp_path, *keys):
    p = tmp_path / "keys"
    p.write_text("# comment\n" + "\n".join(keys) + "\n\n")
    return p


def err(status, reason=None, retry=None, per_day=False):
    details = []
    if reason:
        details.append({"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason})
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    if per_day:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
            }
        )
    return httpx.Response(
        status, json={"error": {"code": status, "message": "m", "details": details}}
    )


def test_error_mapping():
    e = g.map_error(err(429, retry="17s"))
    assert isinstance(e, g.RateLimited) and e.retry_after == 17 and not e.per_day
    assert g.map_error(err(429, per_day=True)).per_day
    assert isinstance(g.map_error(err(400, reason="API_KEY_INVALID")), g.InvalidKey)
    assert isinstance(g.map_error(err(403)), g.Forbidden)
    assert isinstance(g.map_error(err(404)), g.ModelNotFound)
    assert isinstance(g.map_error(err(503)), g.ServerError)
    assert isinstance(g.map_error(err(400)), g.BadRequest)


@respx.mock
async def test_key_goes_in_header_not_url():
    route = respx.post(f"{BASE}/v1beta/models/m1:generateContent").mock(
        return_value=httpx.Response(200, json={"candidates": []})
    )
    client = g.GeminiClient(BASE)
    await client.generate(KEY_A, "m1", {"contents": []})
    req = route.calls.last.request
    assert req.headers["x-goog-api-key"] == KEY_A
    assert KEY_A not in str(req.url)


@respx.mock
async def test_list_models_paginates():
    respx.get(f"{BASE}/v1beta/models").mock(
        side_effect=[
            httpx.Response(200, json={"models": [{"name": "models/a"}], "nextPageToken": "t"}),
            httpx.Response(200, json={"models": [{"name": "models/b"}]}),
        ]
    )
    models = await g.GeminiClient(BASE).list_models(KEY_A)
    assert [m["name"] for m in models] == ["models/a", "models/b"]


def test_keypool_hot_reload_cooldown_dead(tmp_path):
    clock = Clock()
    db = Database(":memory:")
    path = keyfile(tmp_path, KEY_A)
    pool = KeyPool(path, db, clock=clock)
    a = pool.acquire("m")
    assert a.key == KEY_A
    pool.rate_limited(a.id, "m", 30, per_day=False)
    assert pool.acquire("m") is None
    assert pool.acquire("other") is not None  # cooldown is per (key, model)
    clock.t += 31
    assert pool.acquire("m") is not None
    # Hot reload: add a key by editing the file (with a duplicate line).
    path.write_text(f"{KEY_A}\n{KEY_B}\n{KEY_B}\n")
    os.utime(path, (clock.t + 5, clock.t + 5))
    assert len(pool.keys) == 2
    b = next(k for k in pool.keys if k.key == KEY_B)
    pool.mark_dead(b.id, "API key not valid")
    assert all(k.key != KEY_B for k in [pool.acquire("x"), pool.acquire("x")] if k)
    day = dt.datetime.fromtimestamp(clock.t, dt.UTC).date().isoformat()
    usage = db.key_usage_today(day)
    assert sum(r["rate_limited"] for r in usage) == 1


def test_keypool_round_robin(tmp_path):
    clock = Clock()
    pool = KeyPool(keyfile(tmp_path, KEY_A, KEY_B), clock=clock)
    first = pool.acquire("m").id
    clock.t += 1
    second = pool.acquire("m").id
    assert first != second


def test_per_day_cooldown_until_pacific_midnight():
    s = seconds_until_pacific_midnight(1_700_000_000.0)
    assert 60 <= s <= 86400 + 600


def test_keys_redacted_from_logs(tmp_path, caplog):
    KeyPool(keyfile(tmp_path, KEY_A)).keys  # registers the key
    assert KEY_A not in redact(f"oops {KEY_A} failed")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "key=%s", (KEY_A,), None)
    RedactingFilter().filter(record)
    assert KEY_A not in record.getMessage()
    # Telegram tokens are redacted by pattern even if never registered.
    assert "123456789:" not in redact("GET /bot123456789:" + "A" * 35 + "/getMe")


def test_model_filter_and_ranking():
    models = [
        {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {
            "name": "models/gemini-3-flash-preview",
            "supportedGenerationMethods": ["generateContent"],
        },
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]},
        {
            "name": "models/gemini-2.5-flash-preview-tts",
            "supportedGenerationMethods": ["generateContent"],
        },
        {"name": "models/gemma-3-27b-it", "supportedGenerationMethods": ["generateContent"]},
    ]
    names = rank([m["name"] for m in models if is_candidate(m)])
    assert names == [
        "gemini-2.5-flash",
        "gemini-2.0-flash",
        "gemini-2.5-flash-lite",
        "gemini-3-flash-preview",
        "gemini-2.5-pro",
    ]


class FakeGemini:
    def __init__(self, behaviour):
        self.behaviour = behaviour  # model -> list of responses/exceptions
        self.calls = []

    async def list_models(self, key):
        return [
            {"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
            for m in self.behaviour
        ]

    async def generate(self, key, model, body):
        self.calls.append((key, model))
        seq = self.behaviour[model]
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self):
        pass


PING_OK = {
    "candidates": [
        {"content": {"parts": [{"functionCall": {"name": "ping", "args": {"value": "pong"}}}]}}
    ]
}


async def test_model_manager_daily_picks_working_model(tmp_path):
    clock = Clock()
    db = Database(":memory:")
    pool = KeyPool(keyfile(tmp_path, KEY_A, KEY_B), db, clock=clock)
    fake = FakeGemini(
        {
            "gemini-2.5-flash": [g.Forbidden("not free", 403)],
            "gemini-2.5-flash-lite": [PING_OK],
        }
    )
    mm = ModelManager(fake, pool, db, clock=clock)
    best = await mm.daily()
    assert best == "gemini-2.5-flash-lite"
    status = {m["name"]: m["status"] for m in mm.status()}
    assert status["gemini-2.5-flash"] == "notfree"
    assert status["gemini-2.5-flash-lite"] == "ok"
    assert mm.current() == "gemini-2.5-flash-lite"


async def test_model_manager_dead_after_n_failures_and_pin(tmp_path):
    clock = Clock()
    db = Database(":memory:")
    pool = KeyPool(keyfile(tmp_path, KEY_A), db, clock=clock)
    mm = ModelManager(
        FakeGemini({"gemini-2.5-flash": [PING_OK]}),
        pool,
        db,
        max_failures=3,
        clock=clock,
        pinned="gemini-x",
    )
    await mm.discover()
    assert mm.candidates()[0] == "gemini-x"  # pin first while not dead
    for _ in range(3):
        mm.failure("gemini-x", "boom")
    assert "gemini-x" not in mm.candidates()
    mm.gone("gemini-2.5-flash")
    assert mm.needs_discovery
    assert json.dumps(mm.status())


def test_db_audit_counts(tmp_path):
    db = Database(tmp_path / "x.db")
    db.audit(1, "delete", "t", 5, False)
    db.audit(1, "delete", "t", 5, True)
    assert db.count_actions(1, ["delete"], 0) == 1
    assert db.bytes_deleted_since(0) == 5
    assert db.one("PRAGMA journal_mode")[0] == "wal"


@pytest.mark.parametrize("raw,expect", [("17s", 17.0), ("1.5s", 1.5), ("x", None)])
def test_duration(raw, expect):
    assert g._parse_duration(raw) == expect
