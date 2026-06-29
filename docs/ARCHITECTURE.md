# Architecture — GCEX Pilot (Tilicho Credit Assist)

A code-first POC on **Google's Gemini Enterprise for Customer Experience (CES)**: a
lending support agent that answers real account questions, grounds policy answers in a
knowledge base, stays compliant, and turns every conversation into analytics.

## Diagram

```
                       Borrower  (chat today, voice-ready)
                          │
                          ▼
        ┌──────────────────────────────────────────────┐
        │  Web chat UI + Insights dashboard  (browser)  │   our code
        │  shows tool / grounding badges per reply       │
        └──────────────────────────────────────────────┘
                          │  POST /chat
                          ▼
        ┌──────────────────────────────────────────────┐
        │  Cloud Run · FastAPI                           │   our code
        │  serves UI · chat proxy · hosts mock lending API│
        └──────────────────────────────────────────────┘
                          │  runSession
                          ▼
        ┌──────────────────────────────────────────────┐
        │  Gemini Enterprise for CX — AGENT   (Pillar 1) │   Google product
        │  Gemini · playbook · guardrail · RAG          │
        └──────────────────────────────────────────────┘
            │ OpenAPI tool                 │ fileSearch (RAG)
            ▼                              ▼
   ┌─────────────────────┐       ┌─────────────────────────┐
   │ Mock lending API     │◄─key─ │ Vertex AI RAG Engine     │
   │ account·payoff·ticket│ Secret│ grounds policy answers   │
   │ (on Cloud Run)       │ Mgr   └─────────────────────────┘
   └─────────────────────┘                 ▲ docs
                                    ┌───────────────┐
                                    │ Cloud Storage  │  policy corpus
                                    └───────────────┘

   Agent  ──logs every turn──►  BigQuery export (Pillar 4)  ──analytics──►  Insights dashboard
```

## Components

| Component | What it is | Why |
|---|---|---|
| **Web chat UI + Insights dashboard** | Two pages served by our backend | Clickable demo; badges make the agent's tool calls + grounding visible |
| **Cloud Run · FastAPI** | Our backend | Serves the UI, proxies chat to CES (server-side auth), and hosts the mock lending API the agent's tools call |
| **CES agent** | Gemini Enterprise for CX app + playbook agent | The brain: understands intent, calls tools, grounds answers, enforces guardrail. **Pillar 1** |
| **OpenAPI toolset** | Tool that calls the lending API | Lets the agent take real actions (lookup, payoff, raise ticket) |
| **Secret Manager** | Holds the lending-API key | Tool auth without secrets in code; CES service agent reads it |
| **Vertex AI RAG Engine** | Serverless RAG corpus | Grounds policy/fee answers in real docs (cited, not hallucinated) |
| **Cloud Storage** | Policy doc corpus | Source of truth imported into the RAG corpus |
| **BigQuery export** | CES auto-export of conversations | Captures 100% of conversations for analytics. **Pillar 4** |

## Request flow
1. Borrower types in the web UI → `POST /chat` to Cloud Run.
2. Cloud Run calls the CES agent (`runSession`).
3. Agent reasons (Gemini) and either:
   - calls the **lending API** (OpenAPI tool, authed via Secret Manager), or
   - **grounds** the answer via the RAG Engine corpus.
4. The **compliance guardrail** checks the reply; it returns to the UI with tool/grounding badges.
5. The conversation is **exported to BigQuery**; the **Insights dashboard** queries it.

## Suite mapping
- **Pillar 1 — Conversational Agents:** the CES agent (built deep).
- **Pillar 4 — Conversational Insights:** BigQuery export → dashboard.
- **Pillar 2 — Voice:** Google Chirp-3 HD TTS + browser speech-to-text, as a feature inside the web chat. (CCaaS/telephony is narrated, not wired.)

## Why this design
- **Code-first & reproducible:** `agent/provision_scrapi.py` (CXAS SCRAPI) rebuilds the entire agent;
  `infra/` scripts provision the cloud; everything is in git. Operational maturity, not console clicking.
- **Believable, not a toy:** real tool calls + grounded, cited answers + formatted ₹ amounts.
- **Regulated-fintech angle:** compliance guardrail, fair-practice grounding, and 100% conversation capture into Conversational Insights — differentiated from generic retail demos.

## Live
- Agent chat: `https://tilicho-credit-api-804472053350.us-central1.run.app/`
- Insights dashboard: `…/insights`
