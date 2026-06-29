#!/usr/bin/env bash
# Reproduce the Secret Manager setup that lets the CES OpenAPI tool authenticate to
# the Cloud Run backend with the X-API-Key header. Idempotent.
set -euo pipefail
source "$(dirname "$0")/.env"

echo "== enable Secret Manager =="
gcloud services enable secretmanager.googleapis.com --project="$PROJECT_ID" -q

echo "== ensure CES service identity exists =="
TOKEN=$(gcloud auth print-access-token)
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "https://serviceusage.googleapis.com/v1beta1/projects/${PROJECT_ID}/services/ces.googleapis.com:generateServiceIdentity" \
  >/dev/null
CES_SA="service-${PROJECT_NUMBER}@gcp-sa-ces.iam.gserviceaccount.com"
echo "CES service agent: $CES_SA"

echo "== create / update secret =="
if gcloud secrets describe tilicho-backend-api-key --project="$PROJECT_ID" >/dev/null 2>&1; then
  printf '%s' "$BACKEND_API_KEY" | gcloud secrets versions add tilicho-backend-api-key \
    --data-file=- --project="$PROJECT_ID"
else
  printf '%s' "$BACKEND_API_KEY" | gcloud secrets create tilicho-backend-api-key \
    --data-file=- --replication-policy=automatic --project="$PROJECT_ID"
fi

echo "== grant secretAccessor to CES service agent =="
gcloud secrets add-iam-policy-binding tilicho-backend-api-key \
  --member="serviceAccount:${CES_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project="$PROJECT_ID"

echo "Done. Secret version ref:"
echo "  projects/${PROJECT_NUMBER}/secrets/tilicho-backend-api-key/versions/latest"
