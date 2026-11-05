#!/usr/bin/env bash
# End-to-end check of the running compose stack: webhook -> draft -> approve -> delivered once.
# Usage: docker compose up -d && scripts/e2e.sh   (OPERATOR_PASSWORD=... if the UI is protected)
# Step numbers match the "e2e: крок N" references in docs/spec.md.
set -euo pipefail
cd "$(dirname "$0")/.."

BASE=${BASE:-http://localhost:8000}
AUTH=(-u "op:${OPERATOR_PASSWORD:-}")
EVT="evt_e2e_$(date +%s%N)"
TICKET="T-$EVT"

sql() { docker compose exec -T db psql -U postgres support -tAc "$1"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok   $*"; }
wait_status() {  # wait_status <seconds> <status>...
  local deadline=$((SECONDS + $1)); shift
  while ((SECONDS < deadline)); do
    s=$(sql "SELECT status FROM tickets WHERE event_id = '$EVT'")
    for want in "$@"; do [[ $s == "$want" ]] && return 0; done
    sleep 1
  done
  fail "ticket is '$s', expected one of: $*"
}
post_event() {
  curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/webhooks/helpdesk" -H 'Content-Type: application/json' -d "$1"
}

# 1. health
[[ $(curl -s "$BASE/health") == OK ]] || fail "/health"
ok "1 /health -> OK"

# 2. concurrent duplicates make one ticket; foreign reply_url host is refused
body=$(printf '{"event_id":"%s","ticket_id":"%s","customer_email":"e2e@example.com","message":"Не можу експортувати звіт у PDF","reply_url":"http://app:8000/mock-helpdesk/tickets/%s/replies"}' "$EVT" "$TICKET" "$TICKET")
for _ in 1 2 3 4 5; do post_event "$body" > /dev/null & done; wait
[[ $(sql "SELECT count(*) FROM tickets WHERE event_id = '$EVT'") == 1 ]] || fail "duplicates created extra tickets"
code=$(post_event "${body/http:\/\/app:8000/http://169.254.169.254}")
[[ $code == 422 ]] || fail "foreign reply_url host accepted ($code)"
ok "2 5 concurrent duplicates -> 1 ticket; foreign reply_url -> 422"

# 3. the worker drafts a reply with KB links (no-key mode takes 5-30 s)
wait_status 90 draft
id=$(sql "SELECT id FROM tickets WHERE event_id = '$EVT'")
sql "SELECT draft FROM tickets WHERE id = $id" | grep -q "/kb/export-pdf" || fail "draft has no link to the PDF article"
ok "3 draft ready with a link to /kb/export-pdf"

# 4. approve twice (double click) queues one send
for _ in 1 2; do curl -s -o /dev/null "${AUTH[@]}" -X POST "$BASE/tickets/$id/approve"; done
[[ $(sql "SELECT status FROM tickets WHERE id = $id") =~ ^(approved|sent|unconfirmed)$ ]] || fail "approve did not work"
ok "4 approved"

# 5. delivered despite mock 503s; an unconfirmed send is resent by "the operator"
for _ in 1 2 3; do
  wait_status 180 sent unconfirmed
  [[ $s == sent ]] && break
  echo "     unconfirmed (mock answered late), resending as the operator would"
  curl -s -o /dev/null "${AUTH[@]}" -X POST "$BASE/tickets/$id/resend"
done
[[ $s == sent ]] || fail "not delivered after 3 resends"
ok "5 sent (attempts: $(sql "SELECT attempts FROM tickets WHERE id = $id"))"

# 6. the helpdesk shows the customer exactly one reply
[[ $(sql "SELECT count(*) FROM mock_replies WHERE idempotency_key = 'reply-$id'") == 1 ]] || fail "customer got more than one reply"
ok "6 customer sees one reply (deliveries: $(sql "SELECT deliveries FROM mock_replies WHERE idempotency_key = 'reply-$id'"))"

echo "e2e passed"
