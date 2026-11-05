import secrets
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, HttpUrl

from app import db, kb, mock_helpdesk, settings

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
basic = HTTPBasic(auto_error=False)


def operator_auth(credentials: HTTPBasicCredentials | None = Depends(basic)) -> None:
    if not settings.OPERATOR_PASSWORD:
        return
    if credentials is None or not secrets.compare_digest(
        credentials.password.encode(), settings.OPERATOR_PASSWORD.encode()
    ):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


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


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(operator_auth)])
async def operator_page(request: Request):
    async with db.pool.connection() as conn:
        # shortcut: last 200 tickets, no pagination or filters; add them when operators work through real volume
        cur = await conn.execute(
            "SELECT *, coalesce(locked_until > now(), false) AS busy FROM tickets "
            "ORDER BY status = 'draft' DESC, id DESC LIMIT 200"
        )
        tickets = await cur.fetchall()
    return templates.TemplateResponse(request, "operator.html", {"tickets": tickets})


# Both transitions only fire from 'draft', so a double click or two operators cannot queue a second send.
@app.post("/tickets/{ticket_id}/approve", dependencies=[Depends(operator_auth)])
async def approve(ticket_id: int):
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE tickets SET status = 'approved', attempts = 0, next_attempt_at = now(), updated_at = now() "
            "WHERE id = %s AND status = 'draft'",
            [ticket_id],
        )
    return RedirectResponse("/", status_code=303)


@app.post("/tickets/{ticket_id}/reject", dependencies=[Depends(operator_auth)])
async def reject(ticket_id: int):
    async with db.pool.connection() as conn:
        await conn.execute(
            "UPDATE tickets SET status = 'rejected', updated_at = now() WHERE id = %s AND status = 'draft'",
            [ticket_id],
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
