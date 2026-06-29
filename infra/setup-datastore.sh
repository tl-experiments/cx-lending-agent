#!/usr/bin/env bash
# Reproducible RAG setup via Discovery Engine data store: bucket + corpus upload + data
# store + document import. NOTE: this path was SUPERSEDED by Vertex AI RAG Engine (see
# infra/setup-ragengine.sh) after a "Web MDU" indexing bug on this project; kept for
# reference. The live agent grounds via the RAG Engine corpus, not this data store.
set -euo pipefail
HERE="$(dirname "$0")"; source "$HERE/.env"
BUCKET="${DATA_BUCKET}"                 # gs://gcex-pilot-16862-grounding
DS_ID="${DATA_STORE_ID}"               # tilicho-policy
LOC="${DISCOVERY_LOCATION:-global}"
TOKEN=$(gcloud auth print-access-token)
DE="https://discoveryengine.googleapis.com/v1"
COLL="projects/${PROJECT_ID}/locations/${LOC}/collections/default_collection"

echo "== bucket + upload (.txt for reliable ingestion) =="
gcloud storage buckets create "$BUCKET" --project="$PROJECT_ID" --location=us-central1 2>/dev/null || true
tmp=$(mktemp -d)
for f in "$HERE/../data"/*.md; do cp "$f" "$tmp/$(basename "${f%.md}").txt"; done
gcloud storage cp "$tmp"/*.txt "$BUCKET/txt/"

echo "== create data store (GENERIC, chat, content-required) =="
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "${DE}/${COLL}/dataStores?dataStoreId=${DS_ID}" \
  -d '{"displayName":"Tilicho Policy KB","industryVertical":"GENERIC","solutionTypes":["SOLUTION_TYPE_CHAT"],"contentConfig":"CONTENT_REQUIRED"}' \
  | grep -o '"name": *"[^"]*"' | head -1 || true

echo "== import documents from GCS (content schema) =="
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "${DE}/${COLL}/dataStores/${DS_ID}/branches/0/documents:import" \
  -d "{\"gcsSource\":{\"inputUris\":[\"${BUCKET}/txt/*.txt\"],\"dataSchema\":\"content\"},\"reconciliationMode\":\"INCREMENTAL\"}" \
  | grep -o '"name": *"[^"]*"' | head -1

echo ""
echo "Data store full name (set DATA_STORE_FULL in .env to this):"
echo "  ${COLL}/dataStores/${DS_ID}"
echo "Indexing takes a few minutes. Check docs with:"
echo "  curl -s -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" \\"
echo "    \"${DE}/${COLL}/dataStores/${DS_ID}/branches/0/documents\" | grep -c '\"id\"'"
