#!/usr/bin/env bash
# Smoke-test the mock lending backend. Assumes it's running on :8080.
set -euo pipefail
BASE="${BASE:-http://localhost:8080}"
KEY="${API_KEY:-demo-key-gcex}"

echo "== health ==";            curl -fsS "$BASE/health"; echo
echo "== account TL-1001 ==";   curl -fsS -H "X-API-Key: $KEY" "$BASE/accounts/TL-1001"; echo
echo "== payoff TL-1001 ==";    curl -fsS -H "X-API-Key: $KEY" "$BASE/accounts/TL-1001/payoff"; echo
echo "== delinquent TL-1003 =="; curl -fsS -H "X-API-Key: $KEY" "$BASE/accounts/TL-1003"; echo
echo "== create ticket ==";     curl -fsS -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"loan_id":"TL-1003","category":"restructuring","message":"Lost job, need lower EMI"}' \
  "$BASE/tickets"; echo
echo "== auth fails without key (expect 401) =="; \
  curl -s -o /dev/null -w "%{http_code}\n" "$BASE/accounts/TL-1001"
