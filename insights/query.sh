#!/usr/bin/env bash
# Run the Conversational Insights queries against the CES BigQuery export.
set -euo pipefail
HERE="$(dirname "$0")"; source "$HERE/../infra/.env"
run(){ echo "== $1 =="; bq --project_id="$PROJECT_ID" query --use_legacy_sql=false --format=pretty < "$HERE/$2"; echo; }
run "Call drivers (top contact reasons)" call_drivers.sql
run "Transcripts (QA review)"            transcripts.sql
