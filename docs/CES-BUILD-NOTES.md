# CES build notes — how the agent is actually wired (verified by building it)

Hard-won, **verified-on-the-API** knowledge for Gemini Enterprise for Customer
Experience (CES, `ces.googleapis.com`). This is the "every nook" reference — what the
docs don't make obvious, learned by provisioning a working agent.

## API surface
- Base: `https://ces.googleapis.com/v1beta` (also `v1`). Title: *Gemini Enterprise
  for Customer Experience API*.
- **Location is `us`** for our project. `global` and `us-central1` return
  PERMISSION_DENIED / not-found. Parent = `projects/{PROJECT}/locations/us`.
- Auth: standard ADC bearer token (`gcloud auth print-access-token`), scope
  `cloud-platform`. No special client library needed — plain REST works.

## Resource model
```
App (the "agent app"/project; async create → Operation)
├── toolsets/      ← OpenAPI tools live here (NOT apps/tools)
├── agents/        ← the playbooks (instruction + llmAgent + toolsets[])
├── tools/         ← data-store / python / function tools (individual)
├── guardrails/    ├── examples/   ├── sessions/ (runSession, streamRunSession)
├── deployments/   ├── versions/   ├── conversations/   └── evaluations/
```
- `App.rootAgent` = entry-point agent resource name (set via PATCH after the agent
  exists).

## Gotchas that cost real time (now encoded in `agent/provision_scrapi.py`)
1. **App create is asynchronous** — returns a long-running `Operation`. Sub-resource
   creation 404s ("Parent resource does not exist") until you poll the op to `done`.
2. **OpenApiTool can't be created individually** — `apps.tools.create` with an
   `openApiTool` returns *"Creating tools of type OpenApiTool is not supported. Please
   use OpenApi Toolsets instead."* → create an **`openApiToolset`** (`apps.toolsets`).
   It auto-generates one tool per OpenAPI `operationId`.
3. **`toolIds` must be explicit** on the agent's toolset reference:
   `agent.toolsets = [{toolset, toolIds:[...operationIds]}]`. With an **empty
   `toolIds`, NO tools are exposed** to the planner and every tool call fails with
   `"unexpected function call"` (the model emits a call the runtime won't accept).
   This was the single biggest blocker. Tool ids = the OpenAPI operationIds.
4. **OpenAPI must be 3.0.x** (FastAPI emits 3.1.0 → rejected). And the YAML parser is
   strict: an **unquoted colon** in a description (`fallback: http://…`) breaks it
   with *"mapping values are not allowed here"*.
5. **Tool auth = Secret Manager.** `apiKeyConfig` needs `apiKeySecretVersion` (a
   Secret Manager version), `keyName`, `requestLocation: HEADER` — not a plaintext
   key. Grant `roles/secretmanager.secretAccessor` to the CES service agent
   `service-{PROJECT_NUMBER}@gcp-sa-ces.iam.gserviceaccount.com` (create it via the
   Service Usage `:generateServiceIdentity` REST call — `gcloud beta` may be absent).
6. **Free-trial Gemini quota is low** → `runSession` intermittently returns **429**.
   The provisioner/chat back off and retry. For demos, request a quota bump or pace
   calls.

## Verifying without the model
`apps:executeTool` runs a tool directly (bypassing the planner) — invaluable to prove
the tool→backend→Secret-Manager chain independent of LLM behavior:
```
POST {app}:executeTool { "toolsetTool": {"toolset": <name>, "toolId": "getAccountSummary"},
                         "args": {"loan_id": "TL-1001"} }
```

## Testing a conversation
`POST {app}/sessions/{id}:runSession { "config": {}, "inputs": [{"text": "..."}] }`
- Reply text is in `outputs[].text`.
- `outputs[].diagnosticInfo.rootSpan` is a full trace (LLM spans, tool spans, and the
  `"unexpected function call"` attribute when a call is rejected) — primary debugging
  tool.
- runSession is stateless per call; pass `config.historicalContexts` for multi-turn.

## What's verified working
App + playbook agent + OpenAPI toolset (3 tools) + Cloud Run backend with Secret
Manager-backed API-key auth. Live, correct, grounded answers for: EMI/account lookup,
foreclosure payoff quote, and hardship→restructuring (with empathetic, consent-first,
compliant behavior). Re-running `provision_scrapi.py provision` is idempotent.

## Grounding / RAG — SOLVED via Vertex AI RAG Engine (not Discovery Engine)
Working path: CES **`fileSearchTool`** → Vertex AI **RAG Engine** (`ragCorpora`).
Reproducible in `infra/setup-ragengine.sh`; auto-attached by `provision_scrapi.py` when
`RAG_CORPUS` is set. Verified: policy questions return correct, grounded answers with
`filesearch`/`source` markers.

Setup gotchas (all encoded in the script):
- RAG Engine on a new project rejects Spanner mode in us-central1/us-east1/us-east4
  → **switch to Serverless mode** (`PATCH .../ragEngineConfig
  {"ragManagedDbConfig":{"serverless":{}}}`).
- Serverless RAG uses Vector Search under the hood → **enable
  `vectorsearch.googleapis.com`** (else corpus create fails "Vector Search API has not
  been used").
- `fileSearchTool.fileCorpus` = full `projects/.../locations/us-central1/ragCorpora/{id}`;
  `corpusType: USER_OWNED` (we created the corpus). Tool **display names must be unique**
  per app (deleted the old `policy_kb` data-store tool first).

### Discovery Engine data store — ABANDONED
First RAG attempt. Import failed for txt AND html with an internal
*"Missing required Web MDU fields … SPANION_section_url"* indexing error, independent
of file format and of the CES-auto-created engine. Looks like a Discovery Engine
GENERIC-content bug on this project; RAG Engine sidesteps it. (Also learned: attaching
a `dataStoreTool` makes CES auto-create an `Engine` over the store, and a deleted data
store's ID is locked for ~2h.)

## Another runtime quirk: transient 400 after agent PATCH
For ~10–60s after any `agents.patch` (tools/instruction/etc.), `runSession` returns
`400 INVALID_ARGUMENT` ("Request contains an invalid argument") while the agent
re-settles, then recovers to 200. `provision_scrapi.py chat` now retries on 400.

## Quota (free-trial → paid)
CES uses `gemini-2.5-flash-001` + `gemini-2.5-flash-lite` in location `us`. Trial caps
generate-content to ~5 req/min/model. Upgrading to paid lifts the ceiling but the
per-model default stays low; filed Cloud Quotas increase requests (base_models
`gemini-2.5-flash`, `gemini-2.5-flash-lite`, value 60) — may need console/support to
grant. Note `gemini-2.5-flash` is NOT settable as an explicit model in location `us`.

## Status
Web chat, in-chat voice (Chirp-3 TTS + browser STT), and Conversational Insights (native CCAI,
surfaced on the dashboard) are built.
