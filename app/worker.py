"""Background worker: drafts replies for new tickets and delivers approved ones. Run: python -m app.worker"""

import asyncio
import logging

import httpx

from app import db, kb, llm, settings

log = logging.getLogger("worker")

# Lease outlives the slowest path: waiting for a rate-limit slot (~60 s) + LLM call (timeout 120 s).
# Approved replies go first: a customer is waiting on them, and they cost no LLM call.
CLAIM = """
UPDATE tickets SET locked_until = now() + interval '5 minutes', attempts = attempts + 1, updated_at = now()
WHERE id = (
    SELECT id FROM tickets
    WHERE status IN ('new', 'approved') AND next_attempt_at <= now()
      AND (locked_until IS NULL OR locked_until < now())
    ORDER BY status = 'approved' DESC, next_attempt_at
    LIMIT 1 FOR UPDATE SKIP LOCKED)
RETURNING *"""


async def claim() -> dict | None:
    async with db.pool.connection() as conn:
        return await (await conn.execute(CLAIM)).fetchone()


async def execute(sql: str, params: list) -> None:
    async with db.pool.connection() as conn:
        await conn.execute(sql, params)


async def generate(ticket: dict) -> None:
    articles = kb.search(ticket["message"])
    text = await llm.draft_reply(ticket["message"], articles)
    if articles:
        text += "\n\nКорисні статті:\n" + "\n".join(f"- {a['title']}: {a['url']}" for a in articles)
    await execute(
        """UPDATE tickets SET status = 'draft', draft = %s, attempts = 0, locked_until = NULL,
                  last_error = NULL, updated_at = now()
           WHERE id = %s AND status = 'new'""",
        [text, ticket["id"]],
    )


async def send(ticket: dict, http: httpx.AsyncClient) -> None:
    try:
        # The key is stable across resends: a helpdesk that honours it drops the duplicate itself.
        response = await http.post(
            ticket["reply_url"],
            json={"ticket_id": ticket["ticket_id"], "message": ticket["draft"]},
            headers={"Idempotency-Key": f"reply-{ticket['id']}"},
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise  # the request never left: safe to retry
    except httpx.TransportError as e:
        # Sent but no answer (timeout, dropped connection): it may have landed. An automatic resend could
        # show the customer the reply twice, so an operator checks the helpdesk and decides.
        await execute(
            "UPDATE tickets SET status = 'unconfirmed', last_error = %s, locked_until = NULL, updated_at = now() "
            "WHERE id = %s AND status = 'approved'",
            [repr(e)[:1000], ticket["id"]],
        )
        return
    if not response.is_success:
        raise RuntimeError(f"helpdesk answered {response.status_code}")
    await execute(
        "UPDATE tickets SET status = 'sent', locked_until = NULL, last_error = NULL, updated_at = now() "
        "WHERE id = %s AND status = 'approved'",
        [ticket["id"]],
    )


async def handle(ticket: dict, http: httpx.AsyncClient) -> None:
    try:
        if ticket["status"] == "approved":
            await send(ticket, http)
        else:
            await generate(ticket)
    except Exception as e:
        give_up = ticket["attempts"] >= settings.MAX_ATTEMPTS
        log.warning("ticket %s attempt %s failed: %r", ticket["id"], ticket["attempts"], e)
        await execute(
            """UPDATE tickets SET status = CASE WHEN %s THEN 'failed' ELSE status END,
                      last_error = %s, locked_until = NULL, updated_at = now(),
                      next_attempt_at = now() + make_interval(secs => %s)
               WHERE id = %s AND status IN ('new', 'approved')""",
            [give_up, repr(e)[:1000], min(5 * 2 ** ticket["attempts"], 600), ticket["id"]],
        )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    await db.init()
    slots = asyncio.Semaphore(settings.WORKER_CONCURRENCY)
    running: set[asyncio.Task] = set()

    async def run(ticket: dict, http: httpx.AsyncClient) -> None:
        try:
            await handle(ticket, http)
        finally:
            slots.release()

    log.info("worker started, concurrency=%s, llm key %s", settings.WORKER_CONCURRENCY,
             "set" if settings.ANTHROPIC_API_KEY else "missing (static replies)")
    # 10 s send timeout: shorter than the mock's slow answer, so the ambiguous-timeout path is exercised
    async with httpx.AsyncClient(timeout=10) as http:
        while True:
            await slots.acquire()
            ticket = await claim()
            if ticket is None:
                slots.release()
                await asyncio.sleep(1)
                continue
            log.info("ticket %s: %s, attempt %s", ticket["id"],
                     "sending" if ticket["status"] == "approved" else "drafting", ticket["attempts"])
            task = asyncio.create_task(run(ticket, http))
            running.add(task)
            task.add_done_callback(running.discard)


if __name__ == "__main__":
    asyncio.run(main())
