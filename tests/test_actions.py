"""Pending actions: nonce, expiry, same-user and exactly-once enforcement."""

import asyncio

from smarrtbot.db import Database
from smarrtbot.flows.actions import ClaimStatus, PendingActions


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def make():
    clock = Clock()
    return PendingActions(Database(":memory:"), ttl_seconds=300, clock=clock), clock


def test_only_requester_can_confirm():
    pa, _ = make()
    n = pa.create("delete", 1, 10, {"x": 1})
    assert pa.claim(n, 2).status == ClaimStatus.NOT_REQUESTER
    # A wrong-user tap does not consume the action.
    c = pa.claim(n, 1)
    assert c.status == ClaimStatus.OK and c.action.payload == {"x": 1}


def test_exactly_once():
    pa, _ = make()
    n = pa.create("delete", 1, 10, {})
    assert pa.claim(n, 1).status == ClaimStatus.OK
    assert pa.claim(n, 1).status == ClaimStatus.USED


def test_expiry(monkeypatch):
    pa, clock = make()
    n = pa.create("delete", 1, 10, {})
    clock.t += 301
    # The db stores wall-clock expiry; emulate by moving the row into the past.
    pa.db.execute("UPDATE pending_actions SET expires_at=? WHERE nonce=?", (clock.t - 1, n))
    assert pa.claim(n, 1).status == ClaimStatus.EXPIRED
    assert pa.claim(n, 1).status == ClaimStatus.USED


def test_unknown_and_cancel():
    pa, _ = make()
    assert pa.claim("nope", 1).status == ClaimStatus.UNKNOWN
    n = pa.create("add", 1, 10, {})
    assert pa.cancel(n, 2).status == ClaimStatus.NOT_REQUESTER
    assert pa.cancel(n, 1).status == ClaimStatus.OK
    assert pa.claim(n, 1).status == ClaimStatus.USED


def test_nonce_is_random_and_short():
    pa, _ = make()
    nonces = {pa.create("add", 1, 1, {}) for _ in range(50)}
    assert len(nonces) == 50
    assert all(len(n) == 16 for n in nonces)


async def test_concurrent_claims_single_winner():
    pa, _ = make()
    n = pa.create("delete", 1, 10, {})

    async def claim():
        await asyncio.sleep(0)
        return pa.claim(n, 1).status

    results = await asyncio.gather(*[claim() for _ in range(10)])
    assert results.count(ClaimStatus.OK) == 1
