#!/usr/bin/env bash
# Reproducible grounding via Vertex AI RAG Engine (the WORKING RAG path for CES).
# Creates a serverless RAG corpus and imports the policy corpus from GCS. Then set
# RAG_CORPUS in infra/.env to the printed corpus name and run:
#   python agent/provision_scrapi.py provision    # attaches the fileSearchTool
#
# Why RAG Engine (not Discovery Engine data store): the data-store import failed with
# an internal "Web MDU" indexing error on this project; RAG Engine works cleanly.
set -euo pipefail
HERE="$(dirname "$0")"; source "$HERE/.env"
REGION="${RAG_REGION:-us-central1}"
BASE="https://${REGION}-aiplatform.googleapis.com/v1"
tok(){ gcloud auth print-access-token; }
PY(){ "$HERE/../backend/.venv/bin/python3" -c "$@"; }

echo "== enable required APIs (Vertex AI + Vector Search) =="
gcloud services enable aiplatform.googleapis.com vectorsearch.googleapis.com --project="$PROJECT_ID" -q

echo "== switch RAG Engine to Serverless mode (new projects lack Spanner capacity) =="
curl -s -X PATCH -H "Authorization: Bearer $(tok)" -H "Content-Type: application/json" \
  "$BASE/projects/$PROJECT_ID/locations/$REGION/ragEngineConfig" \
  -d '{"ragManagedDbConfig":{"serverless":{}}}' >/dev/null
echo "   (serverless mode requested; propagation may take a minute)"

echo "== ensure corpus exists =="
# upload corpus as .txt if not already present
gcloud storage cp "$HERE/../data"/*.md "$DATA_BUCKET/md/" >/dev/null 2>&1 || true
tmp=$(mktemp -d); for f in "$HERE/../data"/*.md; do cp "$f" "$tmp/$(basename "${f%.md}").txt"; done
gcloud storage cp "$tmp"/*.txt "$DATA_BUCKET/txt/" >/dev/null

CORPUS=""
for attempt in $(seq 1 10); do
  COP=$(curl -s -X POST -H "Authorization: Bearer $(tok)" -H "Content-Type: application/json" \
    "$BASE/projects/$PROJECT_ID/locations/$REGION/ragCorpora" \
    -d '{"displayName":"tilicho-policy-rag","description":"Tilicho lending policy KB"}' \
    | PY "import sys,json
try: print(json.load(sys.stdin).get('name',''))
except: print('')")
  [ -z "$COP" ] && { sleep 15; continue; }
  until curl -s -H "Authorization: Bearer $(tok)" "$BASE/$COP" | grep -q '"done": *true'; do sleep 8; done
  CORPUS=$(curl -s -H "Authorization: Bearer $(tok)" "$BASE/$COP" | PY "import sys,json;print(json.load(sys.stdin).get('response',{}).get('name',''))")
  [ -n "$CORPUS" ] && break
  echo "   attempt $attempt: corpus not ready (API may still be propagating)"; sleep 15
done
[ -z "$CORPUS" ] && { echo "ERROR: could not create corpus"; exit 1; }
echo "   CORPUS=$CORPUS"

echo "== import policy docs into corpus =="
IOP=$(curl -s -X POST -H "Authorization: Bearer $(tok)" -H "Content-Type: application/json" \
  "$BASE/$CORPUS/ragFiles:import" \
  -d "{\"importRagFilesConfig\":{\"gcsSource\":{\"uris\":[\"$DATA_BUCKET/txt/\"]}}}" \
  | PY "import sys,json;print(json.load(sys.stdin).get('name',''))")
until curl -s -H "Authorization: Bearer $(tok)" "$BASE/$IOP" | grep -q '"done": *true'; do sleep 10; done
curl -s -H "Authorization: Bearer $(tok)" "$BASE/$CORPUS/ragFiles" \
  | PY "import sys,json;print('imported files:',len(json.load(sys.stdin).get('ragFiles',[])))"

echo ""
echo "Set this in infra/.env, then run: python agent/provision_scrapi.py provision"
echo "  RAG_CORPUS=$CORPUS"
