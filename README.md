# Gemini Enterprise for CX — Lending Agent (POC)

A code-first, reproducible proof-of-concept on **Google's Gemini Enterprise for Customer
Experience** (formerly Customer Engagement Suite / Contact Center AI), built around a
**fintech/lending** demo: *Tilicho Credit Assist*.

It demonstrates the suite end-to-end — a conversational agent with real tool calls, RAG
grounding, a compliance guardrail, in-chat voice, and native Conversational Insights — all
built from code with the official **CXAS SCRAPI** toolkit, and reproducible.

## What's here

| Path | What |
|---|---|
| **[docs/SYSTEM-DESIGN.md](docs/SYSTEM-DESIGN.md)** | **Full system documentation — start here** |
| `docs/` | System design, architecture, evals, security, CES build notes |
| `agent/` | Code-first provisioner (`provision_scrapi.py`, CXAS SCRAPI), agent spec, OpenAPI tool schema, golden + tool evals |
| `backend/` | FastAPI: mock lending API + `/chat` proxy + the web UIs (`/`, `/insights`, `/tour`) |
| `data/` | Synthetic grounding corpus (loan T&Cs, FAQ, fair-practices, grievance) |
| `insights/` | Conversational Insights — pulls the native CCAI analysis into BigQuery + SQL |
| `infra/` | API enablement, RAG-engine setup, env template |
| `tests/` | pytest for the backend (₹ formatting, payoff math, identity gate) |
| `Makefile` | One-command local run + tests |

## What's built

- **Conversational agent (Pillar 1)** — CES app + agent + OpenAPI toolset + compliance guardrail,
  built from code via [`agent/provision_scrapi.py`](agent/provision_scrapi.py). Account lookups,
  payoff quotes and ticket creation are **real tool calls**, after **server-side identity
  verification** (loan ID + last-4 phone; 403 on mismatch).
- **Grounding** — Vertex AI RAG Engine + CES `fileSearchTool`; policy answers are cited, not invented.
- **Voice (in chat)** — 🎤 browser speech-to-text + 🔊 Google Chirp-3 HD TTS, as a feature inside the web chat.
- **Conversational Insights (Pillar 4)** — every conversation auto-ingests into the native
  **Contact Center AI Insights** product (real Google ML sentiment + entities); the dashboard surfaces it.
- **Tested** — golden evals + tool-level assertions + `cxas lint` (see [docs/EVALS.md](docs/EVALS.md)),
  plus backend pytest (`make test`).

**Live demo** (`https://tilicho-credit-api-804472053350.us-central1.run.app`):
- `/` — the agent chat (tools, grounding, guardrail, ₹ formatting, in-chat voice)
- `/insights` — the Conversational Insights dashboard
- `/tour` — the guided walkthrough (what it is, the architecture, the live links)

## Build it

One-time setup — create the SCRAPI venv (Python ≥3.10) and your config:

```sh
python3 -m venv .venv-scrapi && .venv-scrapi/bin/pip install -r agent/requirements.txt
cp infra/env.example infra/.env   # then set PROJECT_ID / PROJECT_NUMBER / BACKEND_URL / RAG_CORPUS
```

Then provision and chat (idempotent):

```sh
.venv-scrapi/bin/python agent/provision_scrapi.py provision
.venv-scrapi/bin/python agent/provision_scrapi.py chat "My loan is TL-1001, phone 4417 — what's my EMI?"
```

## Quickstart (local, no GCP)

```sh
make backend       # run the mock lending API on :8080
make test          # backend unit tests
```

See [docs/SYSTEM-DESIGN.md](docs/SYSTEM-DESIGN.md) for the full architecture and
[docs/EVALS.md](docs/EVALS.md) for the eval story.
