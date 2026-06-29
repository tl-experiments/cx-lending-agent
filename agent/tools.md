# Tools & data store — Tilicho Credit Assist

How the agent acts on the world. Three OpenAPI tools (→ mock backend) and one data
store tool (→ grounding corpus).

## OpenAPI tool — Tilicho Credit Servicing API

- **Spec:** `backend/openapi.json` (generated from the FastAPI app; regenerate with
  `make openapi`).
- **Auth:** API key in header `X-API-Key` (local-dev placeholder `demo-key-gcex`; the
  deployed value is a rotated Secret Manager secret, mounted into the backend, never committed). In CX Agent
  Studio configure this as an API-key auth on the tool. For the cloud demo the
  backend is deployed to Cloud Run and the tool's server URL points there.
- **Operations the agent uses:**

| operationId | Method / path | Purpose | Agent uses when |
|---|---|---|---|
| `getAccountSummary` | GET `/accounts/{loan_id}` | Balance, EMI, next due date, status | "what's my EMI / balance / due date" |
| `getPayoffQuote` | GET `/accounts/{loan_id}/payoff` | Foreclosure quote breakdown | "close my loan early / payoff amount" |
| `createTicket` | POST `/tickets` | Raise restructuring / complaint / KYC / callback | hardship, complaint, KYC, callback |

> Note: in playbooks, tool calls look like `tools.<tool>.<operation>({...})` (code
> block) or are invoked automatically by the model from the instructions. We'll wire
> both: instruction-driven for the demo, one code-block example to show the technique.

## Data store tool — Tilicho policy knowledge

- **Sources:** the four files in `data/` — `loan-terms.md`, `faq.md`,
  `fair-practices-policy.md`, `grievance-redressal.md`.
- **Ingestion:** upload `data/` to a Cloud Storage bucket, create a data store
  (Vertex AI Search / Discovery Engine), attach it to the agent as a data-store tool.
- **Used for:** all policy/info answers (charges, SLAs, prepayment rules, grievance
  process). The agent must ground these answers and cite the source.

## Local → cloud backend swap

- **Local (now):** `http://localhost:8080` for development.
- **Cloud (Phase 1):** deploy `backend/` to Cloud Run; set the tool's server URL to
  the Cloud Run URL so the hosted agent can reach it. (Tracked in `infra/`.)

## Verification

Each operation has a deterministic expected output — see `backend/smoke_test.sh`.
Once the agent is built, run the Phase-1 conversation tests (Examples A–E in
`playbook.md`) via detect-intent and confirm the tool calls + grounded answers.
