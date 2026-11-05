import asyncio
import time

import httpx
import pytest

from app import db, kb, llm, settings, worker
from app.main import app

pytestmark = pytest.mark.anyio

EVENT = {
    "event_id": "evt_test",
    "ticket_id": "T-1",
    "customer_email": "user@example.com",
    "message": "Не можу експортувати звіт у PDF",
    "reply_url": "http://test/mock-helpdesk/tickets/T-1/replies",
}


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def fetchall(sql: str, params: list | None = None) -> list[dict]:
    async with db.pool.connection() as conn:
        return await (await conn.execute(sql, params)).fetchall()


def test_search_finds_the_matching_article():
    assert [a["slug"] for a in kb.search("Не можу експортувати звіт у PDF")] == ["export-pdf"]
    assert kb.search("Привіт, у мене все добре") == []


async def test_concurrent_duplicate_webhooks_create_one_ticket(clean):
    async with client() as c:
        responses = await asyncio.gather(*[c.post("/webhooks/helpdesk", json=EVENT) for _ in range(20)])
    assert {r.status_code for r in responses} == {200}
    assert sorted(r.json()["status"] for r in responses) == ["accepted"] + ["duplicate"] * 19
    assert len(await fetchall("SELECT id FROM tickets")) == 1


async def test_a_job_is_claimed_by_one_worker_only(clean):
    async with client() as c:
        await c.post("/webhooks/helpdesk", json=EVENT)
    claims = await asyncio.gather(*[worker.claim() for _ in range(10)])
    assert sum(c is not None for c in claims) == 1


async def test_rate_limiter_blocks_over_the_limit(clean):
    start = time.monotonic()
    for _ in range(3):
        await llm.acquire_slot(limit=3, window=1)
    assert time.monotonic() - start < 0.5
    await llm.acquire_slot(limit=3, window=1)
    assert time.monotonic() - start >= 0.9


async def test_retried_send_reaches_the_customer_once(clean, monkeypatch):
    monkeypatch.setattr(settings, "MOCK_FAIL_RATE", 0)
    monkeypatch.setattr(settings, "MOCK_SLOW_RATE", 0)
    async with client() as c:
        await c.post("/webhooks/helpdesk", json=EVENT)
    async with db.pool.connection() as conn:
        await conn.execute("UPDATE tickets SET status = 'draft', draft = 'Відповідь'")
    async with client() as c:
        [ticket] = await fetchall("SELECT id FROM tickets")
        assert (await c.post(f"/tickets/{ticket['id']}/approve")).status_code == 303
        assert (await c.post(f"/tickets/{ticket['id']}/approve")).status_code == 303  # double click
        claimed = await worker.claim()
        # first delivery lands, but pretend we never saw the answer (timeout) and retry with the same row
        await worker.send(claimed, c)
        await worker.send(claimed, c)
    [reply] = await fetchall("SELECT * FROM mock_replies")
    assert reply["deliveries"] == 2 and reply["message"] == "Відповідь"
    assert [t["status"] for t in await fetchall("SELECT status FROM tickets")] == ["sent"]
