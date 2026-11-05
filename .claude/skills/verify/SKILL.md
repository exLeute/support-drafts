---
name: verify
description: Prove the service works end to end in Docker before claiming a change is done. Use after any change to app/, tests/, kb/, docker files or the schema, and before every commit.
---

# Verify

A change is done only when all three steps below pass. Do not report "works" from reading code.

1. Rebuild and start the stack:
   `docker compose up -d --build`
   then wait for `curl -s localhost:8000/health` to print `OK`.
2. Unit and integration tests (real Postgres, `support_test` DB):
   `docker compose run --rm test`
3. End-to-end scenario against the running stack:
   `scripts/e2e.sh`
   It checks health, dedup of concurrent duplicates, the reply_url allowlist, drafting with KB links,
   approve, delivery despite mock 503s, and that the customer got exactly one reply.

If a step fails:
- read `docker compose logs --tail 80 app worker` before changing code;
- fix the root cause, then rerun all three steps, not only the failed one.

Report to the user: the pass/fail line of each step, verbatim. If you changed behaviour, say which
requirement in `docs/spec.md` it touches and confirm its "Доказ" column still holds.
