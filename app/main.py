from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl, field_validator

from app import db, kb, mock_helpdesk, settings
from app.auth import operator_auth, same_origin

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# Operator action -> (required current status, new status). Changing status only from the expected one
# makes double clicks and two operators no-ops: a draft can be queued for sending only once.
TRANSITIONS = {
    "approve": ("draft", "approved"),
    "reject": ("draft", "rejected"),
    "resend": ("unconfirmed", "approved"),  # operator checked the helpdesk: the reply did not land
    "delivered": ("unconfirmed", "sent"),  # operator checked the helpdesk: it did land
}


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

    @field_validator("reply_url")
    @classmethod
    def known_helpdesk_host(cls, url: HttpUrl) -> HttpUrl:
        # The worker POSTs here after approval: a forged webhook must not aim it at internal services or at our
        # own operator endpoints. HttpUrl has already lowercased the host and resolved '..' in the path.
        # Self hosts first: listing one in REPLY_URL_HOSTS must not open the operator endpoints.
        if url.host in settings.SELF_HOSTS:
            if (url.path or "").startswith("/mock-helpdesk/"):
                return url
        elif url.host in settings.REPLY_URL_HOSTS:
            return url
        raise ValueError(f"reply_url {url} is not a helpdesk address (REPLY_URL_HOSTS)")


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


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(operator_auth)])
async def operator_page(request: Request):
    async with db.pool.connection() as conn:
        # shortcut: last 200 tickets, no pagination or filters; add them when operators work through real volume
        cur = await conn.execute(
            "SELECT *, coalesce(locked_until > now(), false) AS busy FROM tickets "
            "ORDER BY status IN ('draft', 'unconfirmed') DESC, id DESC LIMIT 200"
        )
        tickets = await cur.fetchall()
    return templates.TemplateResponse(request, "operator.html", {"tickets": tickets})


@app.post("/tickets/{ticket_id}/{action}", dependencies=[Depends(operator_auth), Depends(same_origin)])
async def operator_action(ticket_id: int, action: str):
    if action not in TRANSITIONS:
        raise HTTPException(404)
    current, new = TRANSITIONS[action]
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE tickets SET status = %s, attempts = 0, next_attempt_at = now(), last_error = NULL, "
            "updated_at = now() WHERE id = %s AND status = %s",
            [new, ticket_id, current],
        )
    return RedirectResponse("/", status_code=303)


@app.get("/kb/{slug}", response_class=HTMLResponse)
async def article(slug: str):
    a = kb.BY_SLUG.get(slug)
    if a is None:
        raise HTTPException(404)
    return (
        f"<!doctype html><meta charset=utf-8><title>{escape(a['title'])}</title>"
        f"<h1>{escape(a['title'])}</h1><pre style='white-space:pre-wrap;font:16px sans-serif;max-width:48em'>"
        f"{escape(a['body'])}</pre>"
    )
