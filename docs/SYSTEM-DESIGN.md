# System design & documentation — GCEX Pilot (Tilicho Credit Assist)

The complete reference for how this POC works: architecture, every component, the
request lifecycle, the non-obvious nuances we hit and how we solved them, the
security model, reproducibility, and operations.

> Companion docs: [ARCHITECTURE.md](ARCHITECTURE.md) (one-page diagram),
> [EVALS.md](EVALS.md) (evals + lint), [SECURITY.md](SECURITY.md) (security posture),
> [CES-BUILD-NOTES.md](CES-BUILD-NOTES.md) (CES API gotchas).

## Contents
1. What this is
2. High-level architecture
3. Cloud resources (the concrete inventory)
4. Components in depth
5. Request lifecycle (end to end)
6. The CES agent model & nuances
7. RAG / grounding design & nuances
8. Security & auth model
9. Conversational Insights pipeline
10. Money formatting
11. Quota model (the real bottleneck)
12. Reproducibility — rebuild from zero
13. Operations runbook (commands)
14. Known limitations & open items
15. Repository map

---

## 1. What this is

A **lending customer-experience agent** — "Tilicho Credit Assist" — built **code-first**
on **Google's Gemini Enterprise for Customer Experience (CES)**. A borrower chats; the
agent answers account questions via real tool calls, answers policy questions grounded
in a knowledge base (cited, not hallucinated), stays compliant via a guardrail, and
every conversation is captured to BigQuery for analytics.

Domain is a fictional, PII-safe digital lender. The whole thing is reproducible from
scripts in this repo and is deployed live.

**Suite pillars:** Pillar 1 (Conversational Agents) — built deep; Pillar 2 (Voice) — **Google
Chirp-3 HD TTS** via `/tts` + **browser** speech-to-text, as a feature *inside the web chat*;
Pillar 4 (Conversational Insights) — native CCAI Insights surfaced on a dashboard.
Precise nuances: **STT is the browser's `webkitSpeechRecognition` (Chrome/Edge), not Google
Speech**; **TTS is real Google Chirp-3**. CCaaS/telephony is narrated, not wired.

## 2. High-level architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the diagram. In words: Borrower → web chat
UI → Cloud Run backend (proxy + mock lending API) → **CES agent** → {OpenAPI tool to
the lending API (authed via Secret Manager); fileSearch grounding via Vertex AI RAG
Engine fed from Cloud Storage}. Every conversation → BigQuery → Insights dashboard.

Color model: **our code** (Cloud Run + web), **Gemini Enterprise for CX** (the Google
product), **supporting GCP services** (GCS, Secret Manager, BigQuery, RAG Engine).

## 3. Cloud resources (the concrete inventory)

| Thing | Identifier |
|---|---|
| GCP project | `gcex-pilot-16862` (number `804472053350`) |
| CES app (live) | `projects/gcex-pilot-16862/locations/us/apps/tilicho-credit-scrapi` (built with CXAS SCRAPI) |
| Agent (playbook) | `.../agents/credit-assist` |
| OpenAPI toolset | `.../toolsets/servicing` → tools `getAccountSummary`, `getPayoffQuote`, `createTicket` |
| Grounding tool | `.../tools/policy-rag` (fileSearchTool) |
| Guardrail | `.../guardrails/compliance` (+ input-side `prompt-security`) |
| RAG corpus | `projects/804472053350/locations/us-central1/ragCorpora/6713371708595634176` |
| Cloud Run service | `tilicho-credit-api` (region `us-central1`) |
| Service URL | `https://tilicho-credit-api-804472053350.us-central1.run.app` |
| Secret | `tilicho-backend-api-key` (Secret Manager) |
| GCS bucket | `gs://gcex-pilot-16862-grounding` |
| BigQuery | dataset `tilicho_cx_insights`, table `tilicho-credit-scrapi` |
| Model (in `us`) | inherited default planner model — pinning a specific model is rejected in `us`, so the lean agent shares it (its saving is a shorter prompt, not a separate bucket) |
| CES location | `us` · Cloud Run/RAG/BQ region | `us-central1` / `US` |

## 4. Components in depth

### 4.1 Mock lending backend — `backend/app.py` (FastAPI on Cloud Run)
One service plays **three roles**:
1. **Mock lending API** — the "system of record" the agent's tools call:
   `GET /accounts/{loan_id}` (summary), `GET /accounts/{loan_id}/payoff` (quote),
   `POST /tickets`. Synthetic data for 3 loans (TL-1001/1002/1003). Requires the
   `X-API-Key` header.
2. **Chat proxy** — `POST /chat` relays a user turn to the CES agent (`runSession`)
   using the Cloud Run service account's identity, and returns the reply plus
   **transparency signals** (which tools ran, grounded flag, latency).
3. **Web UI host** — serves the chat UI at `/` and the Insights dashboard at
   `/insights` (+ `/insights/data` which queries BigQuery).

### 4.2 CES agent — `agent/provision_scrapi.py` (CXAS SCRAPI)
An idempotent provisioner builds the agent on CES (`ces.googleapis.com/v1beta`,
location `us`, via Application Default Credentials): app → OpenAPI toolset → fileSearch RAG
tool → guardrails → agents (with instruction) → set root agent.

- **`provision_scrapi.py`** uses Google's official **`cxas-scrapi`** SDK
  (`Apps`/`Tools`/`Guardrails`/`Agents`/`Sessions`) to build app `tilicho-credit-scrapi`,
  with `provision`/`chat`/`info` subcommands. Config, tool schema and a thin REST helper come
  from `agent/ces_common.py`; instructions (CES best-practice XML — `<role>`/`<persona>`/
  `<taskflow>`) from `agent/agent_spec.py`. Tested with **golden evals**, **`cxas test-tools`**
  and **`cxas lint`** (see [EVALS.md](EVALS.md)). Needs Python ≥3.10 (`.venv-scrapi`). CES API
  gotchas are documented in [CES-BUILD-NOTES.md](CES-BUILD-NOTES.md).

### 4.3 Grounding corpus — `data/`
Four synthetic policy docs (loan terms, FAQ, fair-practices code, grievance redressal),
uploaded to GCS and imported into the RAG corpus.

### 4.4 Insights — `insights/`
SQL (`call_drivers.sql`, `transcripts.sql`) + a runner over the BigQuery export. The
dashboard at `/insights` runs the same queries server-side and renders them.

### 4.5 Infra — `infra/`
`enable-apis.sh`, `setup-secret.sh`, `setup-ragengine.sh`, and `.env` (gitignored) hold
the cloud setup. `Makefile` has local targets.

## 5. Request lifecycle (end to end)

A borrower asks "What's my EMI? Loan TL-1001, phone 4417":
1. Browser → `POST /chat` on Cloud Run.
2. Backend mints a token (ADC) and calls
   `…/apps/tilicho-credit-scrapi/sessions/{id}:runSession` with the text.
3. The CES agent (Gemini) reads its instruction, decides to call
   `getAccountSummary(loan_id="TL-1001")`.
4. CES invokes the OpenAPI tool → `GET /accounts/TL-1001` on Cloud Run, sending
   `X-API-Key` resolved from **Secret Manager**.
5. The backend returns the account JSON (including pre-formatted `*_display` fields).
6. The agent composes a reply ("…EMI is ₹8,980…"); the **guardrail** LLM-policy check
   passes (it's a factual statement, not advice).
7. Backend extracts signals from the run trace and returns
   `{reply, tools:["Account lookup"], grounded:false, latency_ms}`.
8. The UI renders the reply + a "🔧 Account lookup" badge + latency.
9. Asynchronously, CES **exports the conversation to BigQuery**; the Insights dashboard
   queries it on demand.

Policy questions follow the same path but step 3 becomes a **fileSearch** call to the
RAG corpus, and the reply carries a "📚 Grounded in policy" badge.

## 6. The CES agent model & nuances

CES models an **App** containing `agents`, `toolsets`, `tools`, `guardrails`,
`examples`, and `sessions`. Hard-won nuances (all encoded in the provisioner):

- **App creation is async** — `apps.create` returns a long-running Operation; sub-
  resources 404 until it's done. We poll the op.
- **OpenAPI tools must be a toolset** — `apps.tools.create` with an `openApiTool` is
  rejected ("use OpenApi Toolsets instead"). Create an `openApiToolset`; it generates
  one tool per OpenAPI `operationId`.
- **`toolIds` must be explicit** — the agent's toolset reference needs
  `toolIds:["getAccountSummary",…]`. With an empty list, **no tools are exposed** and
  every tool call fails with `unexpected function call`. This was the single biggest
  blocker.
- **OpenAPI must be 3.0.x** — FastAPI emits 3.1.0, which the importer rejects; we keep a
  hand-authored 3.0.3 spec (`agent/tool-openapi-3.0.yaml`). The YAML parser is strict:
  an unquoted colon in a description breaks it.
- **Transient 400 after any `agents.patch`** — for ~10–60s after mutating the agent,
  `runSession` returns `400 INVALID_ARGUMENT` while it re-settles, then recovers. The
  `chat` helper and the web `/chat` retry on 400.
- **`executeTool`** runs a tool directly (bypassing the planner) — invaluable for
  isolating "is it the tool or the model?".
- **Idempotency** — the agent is GET-first before create (re-creating an existing agent
  returns 500). Guardrail is patched on re-run to keep its prompt current.

## 7. RAG / grounding design & nuances

Grounding uses CES **`fileSearchTool`** → **Vertex AI RAG Engine** (`ragCorpora`).

**Why not a Discovery Engine data store (our first attempt):** document import failed
for both `.txt` and `.html` with an internal `"Missing required Web MDU fields …
SPANION_section_url"` error — a Discovery Engine GENERIC-content bug on this project,
independent of file format and of the CES-auto-created engine. RAG Engine sidesteps it.

**Nuances getting RAG Engine working on a new project:**
- us-central1/us-east1/us-east4 reject Spanner mode for new projects → **switch to
  Serverless mode** (`PATCH …/ragEngineConfig {"ragManagedDbConfig":{"serverless":{}}}`).
- Serverless RAG uses Vector Search under the hood → **enable
  `vectorsearch.googleapis.com`** or corpus creation fails.
- `fileSearchTool.fileCorpus` = full `…/ragCorpora/{id}`; `corpusType: USER_OWNED`.
- **Tool display names must be unique** per app (we deleted the stale data-store tool
  `policy_kb` before creating the fileSearch tool of the same display name).

**Faithfulness tuning (important for a lending demo):**
- The instruction forces the agent to answer ANY fee/rate/charge/SLA question from the
  knowledge base and never from world knowledge — without this, the model sometimes
  hallucinated plausible-but-wrong numbers (e.g., a "2% + GST" late fee instead of the
  policy's ₹500).
- The retrieval extends to native sentiment/topics/Quality AI via the same corpus or the
  Conversational Insights API (next step).

## 8. Security & auth model

- **Public Cloud Run + app-level API key (deliberate choice):** the service is
  internet-reachable, but the lending endpoints require `X-API-Key`. Data is 100%
  synthetic. (Approved trade-off for a POC demo.)
- **Secret Manager for tool auth:** the key lives in `tilicho-backend-api-key`. The CES
  OpenAPI tool references the secret version via `apiKeyConfig`; the **CES service agent**
  `service-804472053350@gcp-sa-ces.iam.gserviceaccount.com` has
  `roles/secretmanager.secretAccessor`. No secrets in code or git (`infra/.env` is
  gitignored).
- **Backend → CES auth:** the `/chat` proxy uses the Cloud Run runtime service account
  (`804472053350-compute@…`, which has project `Editor`) to call CES — no keys.
- **RAG Engine bucket read:** the Discovery Engine / Vertex service agents were granted
  `storage.objectViewer` on the grounding bucket where needed.
- **Service identities** are created via the Service Usage `:generateServiceIdentity`
  REST call (the `gcloud beta` component wasn't installed).

## 9. Conversational Insights pipeline

`App.loggingSettings.bigqueryExportSettings.enabled = true` (with
`metricAnalysisSettings.llmMetricsOptedOut = false`) exports every conversation turn to
BigQuery dataset `tilicho_cx_insights`, table `` `tilicho-credit-scrapi` `` (partitioned by
`create_time`; nested `messages[]` and `root_spans[]`).

In parallel, Gemini Enterprise for CX **auto-ingests every conversation into the native
Contact Center AI Insights (CCAI) product** at location `us`, which computes **Google ML
sentiment (model `SENTIMENT_MODEL_TYPE_V2`) + entities** per conversation. The native CX Insights
console (`ccai.cloud.google.com`, location **us** — not the default `global`) is the source of truth.

- **Sentiment + entities** come from CCAI: `insights/cci_analyze.py` reads CCAI's own analysis
  via the regional endpoint `us-contactcenterinsights.googleapis.com` (`conversations?view=FULL`,
  after `conversations:bulkAnalyze`) and writes it to BigQuery table `cci_insights`. It does **not**
  run a separate Cloud NL pass — the analysis is CCAI's.
- **Call drivers** (`insights/call_drivers.sql`): keyword classification of the borrower's question
  (Foreclosure / Account-EMI / KYC / Complaint / Hardship / Policy-Fees / Other) → volume + %.
  (Native CCAI topic modelling + Quality-AI scorecards are the next layer — they need a trained
  issue model + conversation volume.)
- **Transcripts** (`insights/transcripts.sql`): borrower question + agent reply per conversation.
- The dashboard (`/insights`) reads BigQuery via REST (server-side ADC, read-only) and renders KPI
  cards, the call-driver chart, the **CCAI sentiment** bar, a **top-entities** widget, and a QA table.
- **Talking point:** 100% of conversations captured and analysed by the real CCAI product — not sampled QA.

## 10. Money formatting

Amounts are formatted at the **source**: the backend's `inr()` helper produces
Indian-grouped rupee strings (`181240 → ₹1,81,240`) returned as `*_display` fields
alongside the raw integers. The agent instruction tells it to quote the `*_display`
values, so formatting is deterministic, not left to the model. (Bonus finding: CES
passes response fields beyond the OpenAPI schema straight to the model, so no toolset
schema change was needed.)

## 11. Quota model (the real bottleneck)

The throttling we hit is **not** the Vertex "requests per minute" quota. It's a
CES-specific token budget:

- `ces.googleapis.com` → **`RunSessionLLMTokensPerMinutePerProjectPerRegionPerBaseModel`**
  = **1,000 tokens/min** per region per base model (`gemini-2.5-flash-001`,
  `gemini-2.5-flash-lite`). One agent turn easily exceeds 1,000 tokens → instant 429.
- Trial→paid upgrade lifts the ceiling but the per-model default stays low; brand-new
  projects often have increase requests **auto-denied** until billing history builds.
- **Fastest unlock:** ask the Google CE rep to expedite an increase (project
  `gcex-pilot-16862`, that quota, region `us-central1`, both base models, 1,000 →
  200,000). Requests are filed and `reconciling`.
- The web UI degrades gracefully (clear "rate limit" message, retries); the Insights
  dashboard is unaffected (BigQuery only).

## 12. Reproducibility — rebuild from zero

```sh
# 0. auth + project (one time), then:
bash infra/enable-apis.sh            # enable required APIs
bash infra/setup-secret.sh           # Secret Manager + CES service-agent access
make backend                          # (local) run the mock API, or deploy:
gcloud run deploy tilicho-credit-api --source backend --region us-central1 \
  --allow-unauthenticated --project gcex-pilot-16862 \
  --service-account=tilicho-cx-runtime@gcex-pilot-16862.iam.gserviceaccount.com \
  --set-secrets=BACKEND_API_KEY=tilicho-backend-api-key:latest
bash infra/setup-ragengine.sh        # serverless RAG corpus + import; prints RAG_CORPUS
# set RAG_CORPUS in infra/.env, then build the agent:
.venv-scrapi/bin/python agent/provision_scrapi.py provision  # CXAS SCRAPI build
# then test it:
.venv-scrapi/bin/cxas lint --app-dir agent/scrapi_app                          # lint
.venv-scrapi/bin/cxas push-eval --app-name "$APP" --file agent/evals/goldens.yaml && \
.venv-scrapi/bin/cxas run --app-name "$APP" --tags account policy hardship memory compliance --wait
```
Config lives in `infra/.env` (gitignored; template `infra/env.example`).

## 13. Operations runbook (commands)

```sh
# Talk to the agent from the CLI (retries 429/400):
.venv-scrapi/bin/python agent/provision_scrapi.py chat "My loan is TL-1001, phone 4417 — what's my EMI?"
.venv-scrapi/bin/python agent/provision_scrapi.py info     # print resource names + root agent

# Backend:
make backend         # run locally on :8080
make backend-test    # smoke-test the lending API
make openapi         # regenerate backend/openapi.json

# Insights:
bash insights/query.sh                          # call drivers + transcripts

# Redeploy after a backend change:
gcloud run deploy tilicho-credit-api --source backend --region us-central1 \
  --allow-unauthenticated --project gcex-pilot-16862 \
  --service-account=tilicho-cx-runtime@gcex-pilot-16862.iam.gserviceaccount.com \
  --set-secrets=BACKEND_API_KEY=tilicho-backend-api-key:latest

# Direct tool test (bypasses the model / quota):
#   POST {app}:executeTool {"toolsetTool":{"toolset":…,"toolId":"getAccountSummary"},
#                           "args":{"loan_id":"TL-1001"}}
```

## 14. Known limitations & open items

- **Quota** — 1,000 tokens/min throttles live multi-turn use until an increase is
  granted (see §11). Demo via pacing or a recording meanwhile.
- **Discovery Engine data store** — abandoned (internal "Web MDU" bug); RAG Engine is the
  working path.
- **Voice STT** is the browser's `webkitSpeechRecognition` (Chrome/Edge only), **not** Google
  Speech — even though `speech.googleapis.com` is enabled. TTS is real Google Chirp-3.
  **CCaaS/telephony** is narrated, not wired.
- **Tickets are in-memory** — restart the backend to reset demo state.
- **Preview surfaces** — some CES/RAG features are preview; behavior may change.

## 15. Repository map

```
GCEX Pilot/
├── README.md                  status + quickstart + live URLs
├── Makefile                   local targets
├── docs/
│   ├── SYSTEM-DESIGN.md       ← this document
│   ├── ARCHITECTURE.md        one-page diagram + flow
│   ├── CES-BUILD-NOTES.md     CES/RAG API gotchas
│   ├── EVALS.md               golden evals + tool tests + lint (SCRAPI)
│   └── SECURITY.md            security posture & trade-offs
├── backend/                   FastAPI: lending API + /chat proxy + web UIs (/, /insights, /tour)
│   ├── app.py  requirements.txt  Procfile  static/
├── agent/                     code-first CES build (CXAS SCRAPI)
│   ├── provision_scrapi.py    ← CXAS SCRAPI build
│   ├── ces_common.py  agent_spec.py   config + shared instructions
│   ├── evals/goldens.yaml  evals/tool_tests.yaml   golden + tool-level evals
│   ├── scrapi_app/            pulled app config (linted IaC artifact)
│   ├── playbook.md  tools.md  tool-openapi-3.0.yaml  requirements.txt
├── data/                      synthetic policy corpus (4 docs)
├── insights/                  BigQuery analytics SQL + runner + README
└── infra/                     enable-apis / setup-secret / setup-ragengine / env
```
