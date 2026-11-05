import asyncio
import time

import httpx
import pytest
from anthropic.types.beta import BetaMessage

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


def fake_claude(monkeypatch, stop_reason: str, content: list[dict]) -> list[dict]:
    """Pretend an API key is set and make Claude answer with a real SDK message object."""
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return BetaMessage.model_validate({
            "id": "msg_test", "type": "message", "role": "assistant", "model": kwargs["model"],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}, "content": content,
        })

    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(llm.client.beta.messages, "create", create)
    return calls


async def draft_once() -> dict:
    async with client() as c:
        await c.post("/webhooks/helpdesk", json=EVENT)
    await worker.handle(await worker.claim(), http=None)
    [ticket] = await fetchall("SELECT * FROM tickets")
    return ticket


async def test_claude_reply_becomes_a_draft_with_kb_links(clean, monkeypatch):
    calls = fake_claude(monkeypatch, "end_turn", [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": "Відкрийте звіт і натисніть «Експорт»."},
    ])
    ticket = await draft_once()
    assert ticket["status"] == "draft"
    assert ticket["draft"].startswith("Відкрийте звіт і натисніть «Експорт».\n\nКорисні статті:")
    assert "/kb/export-pdf" in ticket["draft"]
    [call] = calls
    assert call["fallbacks"] == "default"
    assert EVENT["message"] in call["messages"][0]["content"]
    assert "Як експортувати звіт у PDF" in call["messages"][0]["content"]


@pytest.mark.parametrize("stop_reason, content", [("refusal", []), ("end_turn", [])])
async def test_refused_or_empty_reply_is_retried_not_saved(clean, monkeypatch, stop_reason, content):
    fake_claude(monkeypatch, stop_reason, content)
    ticket = await draft_once()
    assert ticket["status"] == "new" and ticket["draft"] is None
    assert ticket["last_error"] and ticket["locked_until"] is None
    assert ticket["next_attempt_at"] > ticket["created_at"]
