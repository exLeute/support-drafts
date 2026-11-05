"""Stand-in for the real helpdesk's reply API. Misbehaves on purpose: random 503s and answers that arrive too late."""

import asyncio
import random

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app import db, settings

router = APIRouter(prefix="/mock-helpdesk", tags=["mock helpdesk"])

# Longer than the worker's send timeout (10 s), so the sender cannot tell whether the reply landed.
SLOW_SECONDS = 15


class Reply(BaseModel):
    ticket_id: str
    message: str


@router.post("/tickets/{ticket_id}/replies", status_code=201)
async def create_reply(ticket_id: str, reply: Reply, idempotency_key: str | None = Header(None)):
    roll = random.random()
    if roll < settings.MOCK_FAIL_RATE:
        raise HTTPException(503, "random helpdesk failure")
    async with db.pool.connection() as conn:
        await conn.execute(
            """INSERT INTO mock_replies (idempotency_key, ticket_id, message) VALUES (%s, %s, %s)
               ON CONFLICT (idempotency_key) DO UPDATE SET deliveries = mock_replies.deliveries + 1""",
            [idempotency_key, reply.ticket_id, reply.message],
        )
    if roll < settings.MOCK_FAIL_RATE + settings.MOCK_SLOW_RATE:
        await asyncio.sleep(SLOW_SECONDS)  # stored, but the caller has already timed out
    return {"status": "created"}


@router.get("/replies")
async def list_replies():
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM mock_replies ORDER BY id DESC LIMIT 200")
        return await cur.fetchall()
