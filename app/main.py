from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, HttpUrl

from app import db, mock_helpdesk


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    yield
    await db.pool.close()


app = FastAPI(title="Support drafts", lifespan=lifespan)
app.include_router(mock_helpdesk.router)


class HelpdeskEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    ticket_id: str = Field(min_length=1, max_length=200)
    customer_email: str = Field(min_length=3, max_length=320)
    message: str = Field(min_length=1, max_length=20_000)
    reply_url: HttpUrl


@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "OK"


@app.post("/webhooks/helpdesk")
async def helpdesk_webhook(event: HelpdeskEvent):
    # Only persist here: the helpdesk waits 2 s, the LLM takes up to 30 s, so the worker does the rest.
    # The unique event_id turns retries and concurrent duplicates into no-ops.
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO tickets (event_id, ticket_id, customer_email, message, reply_url)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (event_id) DO NOTHING RETURNING id""",
            [event.event_id, event.ticket_id, event.customer_email, event.message, str(event.reply_url)],
        )
        created = await cur.fetchone() is not None
    return {"status": "accepted" if created else "duplicate"}
