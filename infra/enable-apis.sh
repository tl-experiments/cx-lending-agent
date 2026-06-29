#!/usr/bin/env bash
# Enable the APIs the POC needs. Run AFTER the trial-account gate, once a project
# is set. Exact service names for the CES/Insights surfaces are confirmed live in
# Phase 0 — this is the candidate set; we trim/adjust after verification.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "PROJECT_ID not set. Run: gcloud config set project <id>  (or export PROJECT_ID)"; exit 1
fi
echo "Enabling APIs on project: ${PROJECT_ID}"

APIS=(
  dialogflow.googleapis.com          # Conversational Agents (Dialogflow CX) — code-first
  discoveryengine.googleapis.com     # Vertex AI Search / data stores (RAG)
  storage.googleapis.com             # grounding doc bucket
  run.googleapis.com                 # host mock backend
  cloudbuild.googleapis.com          # build/deploy backend
  speech.googleapis.com              # STT (voice)
  texttospeech.googleapis.com        # TTS (voice)
  bigquery.googleapis.com            # Insights export / dashboards
  contactcenterinsights.googleapis.com  # Conversational Insights (confirm name)
  aiplatform.googleapis.com          # Vertex AI (Gemini)
)
# Candidate for the newest CES surface — enable only if Phase 0 confirms it exists:
#   ces.googleapis.com

for api in "${APIS[@]}"; do
  echo "  - ${api}"
  gcloud services enable "${api}" --project="${PROJECT_ID}" || \
    echo "    (skip: ${api} not enabled — verify name)"
done

echo "Done. Verify with: gcloud services list --enabled --project=${PROJECT_ID}"
