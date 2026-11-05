# Support drafts service

Helpdesk webhook → Postgres → worker drafts a reply with Claude using the KB → operator approves in the web UI → worker POSTs it to the helpdesk.
Product requirements and the decisions behind them are in README.md ("Вимоги та рішення"). Keep that table in sync when behaviour changes.

## Layout
- `app/main.py` — FastAPI: webhook, `/health`, operator UI and its status transitions, KB pages
- `app/auth.py` — operator password (HTTP Basic)
- `app/worker.py` — queue loop: claim → generate or send → retry with backoff
- `app/llm.py` — Claude call, global rate limiter, no-key mode
- `app/kb.py` + `kb/*.md` — knowledge base and search
- `app/mock_helpdesk.py` — fake helpdesk reply API (random 503s and late answers)
- `app/schema.sql` — whole schema, applied on startup (idempotent)

## Invariants — do not break
- The webhook does only validation and one INSERT. Never call the LLM or anything slow there (helpdesk times out at 2 s).
- Dedup relies on `UNIQUE(event_id)` + `ON CONFLICT DO NOTHING`. Never "check then insert".
- Every status change is `UPDATE ... WHERE status = '<expected>'`, so concurrent workers and double clicks are no-ops.
- Every LLM request goes through `llm.acquire_slot()`. Keep SDK `max_retries=0` so retries are counted too.
- Without `ANTHROPIC_API_KEY` the real request is still sent; only the answer is replaced by `STATIC_REPLY` after 5–30 s.
- Sends carry `Idempotency-Key: reply-<tickets.id>`; it must stay stable across retries.
- A send that may have landed (timeout, dropped connection after the request left) is never resent automatically: it goes to `unconfirmed` and the operator decides. Only "never connected" errors and 5xx are retried.
- `reply_url` host must be in `settings.REPLY_URL_HOSTS` (checked in the webhook). Don't make the HTTP client follow redirects.
- Everything that shows customer data (operator UI, mock reply list) is behind `auth.operator_auth`.
- Queue state lives only in Postgres (leases via `locked_until`). No in-process queues.

## Conventions
- Raw SQL with psycopg 3 (async pool, dict rows). No ORM.
- Config only via env vars in `app/settings.py`; document new ones in `.env.example` and README.
- UI is server-rendered Jinja (autoescaped). Keep it plain.
- Comments explain why, not what. A known limit gets `# shortcut: <limit>, <when to upgrade>`.

## Workflow
- Behaviour change starts in `docs/spec.md`: add or edit the requirement and its acceptance criterion, then code, then the proof (test or `scripts/e2e.sh` step).
- Done means `/verify` passed (stack, tests, e2e). The Stop hook blocks finishing with failing tests.
- Before committing, run the `reviewer` subagent on the diff and fix or answer every finding.
- Measure before claiming a performance fix; record the numbers in README.

## Commands
- Run: `docker compose up --build`
- Tests: `docker compose run --rm test` (uses the `support_test` DB; tests truncate tables and refuse any other DB)
- E2E on the running stack: `scripts/e2e.sh`
- Without Docker: `DATABASE_URL=postgresql://...  uvicorn app.main:app` and `python -m app.worker`
