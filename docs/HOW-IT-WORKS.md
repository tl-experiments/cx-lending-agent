# What we built & how — in plain terms

A short, non-jargon explainer for talking about the POC.

## What it is
A **customer-support AI agent for a lender** ("Tilicho Credit Assist"), built on
**Google's Gemini Enterprise for Customer Experience** — the exact product the Google
team pitches. A borrower can chat (or talk) to it about their loan; it answers, takes
real actions, hands off to a human, and every conversation turns into analytics.

## What it does (the three things)
1. **Answers account questions** — EMI, balance, due date, foreclosure payoff — by
   making **real lookups**, not guesses. (The numbers come from a live system, formatted
   as ₹1,81,240.)
2. **Answers policy questions correctly** — late fee, prepayment rules, complaint SLAs —
   by reading from a **knowledge base** and quoting it, instead of making things up.
3. **Turns every chat into insight** — what customers contact about, and how they feel
   (sentiment), via the native Contact Center AI Insights product. (the `/insights` screen)

It also **speaks** (Google Chirp-3 HD voice) and **listens** (speech-to-text in the browser,
Chrome/Edge), and **remembers the customer** through the chat via CES session memory (it
won't re-ask for the loan ID once you've given it).

## How it's built (four layers, simply)
1. **The brain — Google's platform.** The agent itself runs on Gemini Enterprise for CX.
   We didn't click it together in a console — we **built it from code using Google's own
   CXAS SCRAPI toolkit** (`cxas-scrapi`): a script creates the agent, its tools, its
   knowledge, and its safety rules. It's repeatable (one command rebuilds the whole thing),
   and it's **tested** — golden evals check the agent answers correctly, and `cxas lint`
   checks the config is well-formed. (See `docs/EVALS.md`.)
2. **The hands — a small service.** A lightweight backend (on Google Cloud Run) gives the
   agent its "tools": look up an account, quote a payoff, raise a ticket. This is the
   only part that's a **stand-in** — in a real deployment it's swapped for the customer's
   actual core systems.
3. **The memory — a knowledge base.** The lender's policy documents are loaded into
   Google's RAG (retrieval) engine, so policy answers are grounded and cited.
4. **The insight — analytics.** Google automatically copies every conversation into
   BigQuery; we run analysis on top (call drivers + Google-ML sentiment) and show it on a
   dashboard.

Everything is **real Google Cloud services** end to end. It's all version-controlled
(in git), so it can be stood up again from scratch.

## What's real vs. a stand-in
- **Real:** the agent, its tools/actions, the knowledge-base grounding, the safety
  guardrail, the voice, the analytics + sentiment — all live Google services.
- **Stand-in:** only the **loan back-office** (account data is synthetic). That's the
  piece a real customer connects to their own systems.
- **Demo data:** the borrowers and conversations are made up — no real customer data.

## What's left (honest)
- Deeper analytics — automatic **topic discovery** and **quality scoring** — need more
  conversation volume / setup. Sentiment is real today.
- **Phone/telephony** (CCaaS) is described, not wired — that needs carrier setup beyond a POC.

## How to see it
- **Run the demo:** `make demo` — plays a clean 5-turn conversation in the terminal.
- **Try it live:** `https://tilicho-credit-api-804472053350.us-central1.run.app`
  (chat) · `/insights` (dashboard) · `/tour` (guided walkthrough)
- **See the agent in Google's console:** https://ces.cloud.google.com → app
  "Tilicho Credit Assist".
- **The build, in code:** `agent/provision_scrapi.py` (built with **CXAS SCRAPI**).
- **The tests:** `agent/evals/goldens.yaml` + `docs/EVALS.md` (golden evals + lint).
- **The full writeup:** `docs/SYSTEM-DESIGN.md`.

## One line to remember
*"It's a real, code-first agent on Google's customer-experience platform — it makes real
lookups, grounds its answers in policy, helps human reps, and analyses every chat. Built
from code, reproducible, running in production."*
