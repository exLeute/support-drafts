-- One row per helpdesk event. The table is also the job queue for the worker:
--   new -> draft (LLM done) -> approved (operator) -> sent (helpdesk 2xx)
--   draft -> rejected;  new/approved -> failed after MAX_ATTEMPTS
-- A job is claimed by setting locked_until (a lease); a crashed worker's lease expires and the job is retried.
CREATE TABLE IF NOT EXISTS tickets (
    id              bigserial PRIMARY KEY,
    event_id        text NOT NULL UNIQUE,
    ticket_id       text NOT NULL,
    customer_email  text NOT NULL,
    message         text NOT NULL,
    reply_url       text NOT NULL,
    status          text NOT NULL DEFAULT 'new',
    draft           text,
    attempts        int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    locked_until    timestamptz,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tickets_queue ON tickets (next_attempt_at) WHERE status IN ('new', 'approved');

-- Sliding-window log of LLM requests, shared by all worker processes.
CREATE TABLE IF NOT EXISTS llm_calls (ts timestamptz NOT NULL DEFAULT clock_timestamp());

-- Replies received by the mock helpdesk. deliveries > 1 means a retry was deduplicated by Idempotency-Key.
CREATE TABLE IF NOT EXISTS mock_replies (
    id              bigserial PRIMARY KEY,
    idempotency_key text UNIQUE,
    ticket_id       text NOT NULL,
    message         text NOT NULL,
    deliveries      int NOT NULL DEFAULT 1,
    created_at      timestamptz NOT NULL DEFAULT now()
);
