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
    "reply_url": "http://app/mock-helpdesk/tickets/T-1/replies",
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


async def test_resend_carries_the_same_idempotency_key(clean, monkeypatch):
    monkeypatch.setattr(settings, "MOCK_FAIL_RATE", 0)
    monkeypatch.setattr(settings, "MOCK_SLOW_RATE", 0)
    async with client() as c:
        await c.post("/webhooks/helpdesk", json=EVENT)
    async with db.pool.connection() as conn:
        await conn.execute("UPDATE tickets SET status = 'draft', draft = 'Відповідь'")
    async with client() as c:
        [ticket] = await fetchall("SELECT id FROM tickets")
        assert (await c.post(f"/tickets/{ticket['id']}/approve")).status_code == 303
        claimed = await worker.claim()
        # the operator resends a reply that had landed: a helpdesk honouring the key keeps one copy
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


@pytest.mark.parametrize("reply_url", [
    "http://169.254.169.254/latest",  # cloud metadata
    "http://app:8000/tickets/1/approve",  # our own operator endpoint
    "http://app/mock-helpdesk/../tickets/1/approve",
    "http://localhost:8000/",
])
async def test_webhook_rejects_reply_url_outside_the_helpdesk(clean, reply_url):
    async with client() as c:
        r = await c.post("/webhooks/helpdesk", json={**EVENT, "reply_url": reply_url})
    assert r.status_code == 422
    assert await fetchall("SELECT id FROM tickets") == []


async def test_listing_our_own_host_as_a_helpdesk_keeps_the_path_rule(clean, monkeypatch):
    monkeypatch.setattr(settings, "REPLY_URL_HOSTS", {"app", "helpdesk.example.com"})
    async with client() as c:
        r = await c.post("/webhooks/helpdesk", json={**EVENT, "reply_url": "http://app/tickets/1/approve"})
    assert r.status_code == 422


async def approved_ticket() -> dict:
    """An approved ticket, claimed by a worker that is about to POST it."""
    async with client() as c:
        await c.post("/webhooks/helpdesk", json=EVENT)
    async with db.pool.connection() as conn:
        await conn.execute("UPDATE tickets SET status = 'approved', draft = 'Відповідь'")
    return await worker.claim()


def helpdesk(outcome: type[httpx.HTTPError] | int) -> httpx.AsyncClient:
    """A helpdesk that answers with this status code or fails with this httpx error."""
    def handler(request):
        if isinstance(outcome, int):
            return httpx.Response(outcome)
        raise outcome("simulated", request=request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def send_once(outcome) -> dict:
    async with helpdesk(outcome) as http:
        await worker.handle(await approved_ticket(), http)
    [ticket] = await fetchall("SELECT * FROM tickets")
    return ticket


@pytest.mark.parametrize("outcome", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, 503, 429])
async def test_send_that_surely_failed_is_retried(clean, outcome):
    ticket = await send_once(outcome)
    assert ticket["status"] == "approved" and ticket["last_error"]
    assert ticket["locked_until"] is None and ticket["next_attempt_at"] > ticket["created_at"]


@pytest.mark.parametrize("outcome", [
    httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.DecodingError, 400, 409,
])
async def test_send_that_may_have_landed_waits_for_the_operator(clean, outcome):
    ticket = await send_once(outcome)
    assert ticket["status"] == "unconfirmed" and ticket["last_error"]
    assert await worker.claim() is None  # never picked up again on its own


async def test_db_failure_after_the_post_waits_for_the_operator(clean, monkeypatch):
    real_execute = worker.execute

    async def execute(sql, params):
        if "'sent'" in sql:
            raise RuntimeError("db went away")  # the helpdesk got the reply, we could not record it
        await real_execute(sql, params)

    monkeypatch.setattr(worker, "execute", execute)
    ticket = await send_once(201)
    assert ticket["status"] == "unconfirmed" and "db went away" in ticket["last_error"]


async def test_failed_attempt_of_a_stale_worker_does_not_touch_a_moved_ticket(clean, monkeypatch):
    async def draft_reply(*args):
        raise RuntimeError("LLM failed")

    monkeypatch.setattr(llm, "draft_reply", draft_reply)
    claimed = await approved_ticket()  # now being sent by one worker
    await worker.handle({**claimed, "status": "new"}, http=None)  # a stale drafting worker fails meanwhile
    [ticket] = await fetchall("SELECT * FROM tickets")
    # clearing the lease would let a second worker send the same reply
    assert ticket["status"] == "approved" and ticket["locked_until"] is not None and ticket["last_error"] is None


async def test_drafting_interrupted_by_a_worker_crash_is_retried(clean):
    async with client() as c:
        await c.post("/webhooks/helpdesk", json=EVENT)
    await worker.claim()  # the worker dies mid-generation and its lease runs out
    async with db.pool.connection() as conn:
        await conn.execute("UPDATE tickets SET locked_until = now() - interval '1 second'")
    assert (await worker.claim())["status"] == "new"


async def test_send_interrupted_by_a_worker_crash_is_not_resent(clean):
    await approved_ticket()  # claimed; the worker dies mid-POST and its lease runs out
    async with db.pool.connection() as conn:
        await conn.execute("UPDATE tickets SET locked_until = now() - interval '1 second'")
    assert await worker.claim() is None
    [ticket] = await fetchall("SELECT * FROM tickets")
    assert ticket["status"] == "unconfirmed" and "worker stopped" in ticket["last_error"]


async def test_operator_resolves_an_unconfirmed_send(clean):
    ticket = await send_once(httpx.ReadTimeout)
    async with client() as c:
        await c.post(f"/tickets/{ticket['id']}/resend")
        assert (await worker.claim())["status"] == "approved"  # queued again by a person, not by the worker
        async with db.pool.connection() as conn:
            await conn.execute("UPDATE tickets SET status = 'unconfirmed', locked_until = NULL, last_error = 'x'")
        await c.post(f"/tickets/{ticket['id']}/delivered")
    [ticket] = await fetchall("SELECT * FROM tickets")
    assert ticket["status"] == "sent" and ticket["last_error"] is None


async def test_operator_actions_only_fire_from_the_expected_status(clean):
    async with client() as c:
        await c.post("/webhooks/helpdesk", json=EVENT)
        async with db.pool.connection() as conn:
            await conn.execute("UPDATE tickets SET status = 'sent', draft = 'Відповідь'")
        [ticket] = await fetchall("SELECT id FROM tickets")
        for action in ("approve", "reject", "resend", "delivered"):  # stale page, double click, second operator
            assert (await c.post(f"/tickets/{ticket['id']}/{action}")).status_code == 303
            # checked after every action: a later one could otherwise undo an earlier wrong transition
            assert [t["status"] for t in await fetchall("SELECT status FROM tickets")] == ["sent"], action
        assert (await c.post(f"/tickets/{ticket['id']}/explode")).status_code == 404
    assert await worker.claim() is None


async def test_operator_actions_refuse_cross_site_requests(clean):
    async with client() as c:
        await c.post("/webhooks/helpdesk", json=EVENT)
        [ticket] = await fetchall("SELECT id FROM tickets")
        url = f"/tickets/{ticket['id']}/reject"
        assert (await c.post(url, headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await c.post(url, headers={"Origin": "http://test"})).status_code == 303


async def test_mock_reply_list_needs_the_operator_password(clean, monkeypatch):
    monkeypatch.setattr(settings, "OPERATOR_PASSWORD", "secret")
    async with client() as c:
        assert (await c.get("/")).status_code == 401
        assert (await c.get("/mock-helpdesk/replies")).status_code == 401
        assert (await c.get("/", auth=("op", "secret"))).status_code == 200
        assert (await c.get("/mock-helpdesk/replies", auth=("op", "secret"))).status_code == 200
