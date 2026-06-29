# Evals & Lint — Tilicho Credit Assist (CXAS SCRAPI)

This agent is built and tested with Google's official **CXAS SCRAPI** toolkit
(`cxas-scrapi`). The provisioner is [`agent/provision_scrapi.py`](../agent/provision_scrapi.py);
this doc records the two engineering-rigor artifacts SCRAPI adds on top of the build:
**golden evals** (does the agent behave correctly?) and **lint** (is the agent config
well-formed?).

- **App under test:** `projects/gcex-pilot-16862/locations/us/apps/tilicho-credit-scrapi`
- **SDK:** `cxas-scrapi==1.4.1` (Python 3.12 venv `.venv-scrapi`)
- **Golden scenarios:** [`agent/evals/goldens.yaml`](../agent/evals/goldens.yaml)
- **Linted config (pulled IaC):** [`agent/scrapi_app/`](../agent/scrapi_app/)

---

## 1. Golden evals

Six "platform golden" scenarios. Each drives the **live** agent with user turns; the
platform runs the agent and an LLM judge scores natural-language **expectations**.

| # | Scenario | What it proves | Behavioral expectations |
|---|----------|----------------|:-----------------------:|
| 1 | EMI & balance lookup | Real tool call, exact figures, ₹ formatting | ✅ pass |
| 2 | Foreclosure payoff quote | Calls payoff tool, explains charge + validity | ✅ pass |
| 3 | Grounded late-payment fee | Answers from policy RAG, not world knowledge (₹500) | ✅ pass |
| 4 | Hardship → restructuring | Empathy, offers a restructuring ticket | ✅ pass |
| 5 | Complaint after ID given | Session memory: no re-verify; files a ticket + SLA | ✅ pass |
| 6 | Declines investment advice | Compliance guardrail holds | ✅ pass |

**Result: 6/6 scenarios pass every behavioral expectation — `0` custom-expectation
failures.** (Common expectations — never asks for card/CVV/OTP/PIN; money always in ₹ —
also pass across all six.)

Both the **console** (Evaluate tab) and the CLI now report **6/6 PASS**.

### One config fix was needed (and why)
Out of the box the platform marked the 4 *tool-using* cases as **failed** — not because the
answers were wrong, but because of its auto **tool-trajectory** metric. That metric compares
the agent's tool calls to a *reference tool trace*, which we deliberately **don't** provide:
CES golden `toolCall` expectations only accept top-level tools (`apps/<app>/tools/<id>`) and
**cannot reference OpenAPI-toolset operations** (`.../toolsets/servicing/tools/...`) — pushing
one is rejected:

```
400 BadRequestException: Path '.../toolsets/servicing/tools/getAccountSummary'
    does not match template '.../apps/{app_id}/tools/{tool_id}'.
```

With an empty reference trace, every real tool call was scored as an "extra/unexpected" call
→ test failed. (The 2 non-tool cases — hardship, declines-advice — passed, since they call
no tool.)

**Fix:** set each evaluation's threshold override to `extra_tool_call_behavior = ALLOW`
(see [`agent/evals/configure_metrics.py`](../agent/evals/configure_metrics.py)). The agent's
real tool calls are now permitted, and the **behavioural LLM expectations** decide pass/fail.
Result: **6 / 6 PASS** in the console. This is honest — the behavioural assertions (the
meaningful checks) still run and all pass; we only stopped penalising tool calls the platform
structurally can't model for OpenAPI toolsets.

### Reproduce
```bash
APP=projects/gcex-pilot-16862/locations/us/apps/tilicho-credit-scrapi
.venv-scrapi/bin/cxas push-eval --app-name "$APP" --file agent/evals/goldens.yaml
.venv-scrapi/bin/python agent/evals/configure_metrics.py      # allow extra tool calls
.venv-scrapi/bin/cxas run --app-name "$APP" \
    --tags account policy hardship memory compliance --wait    # -> 6/6 PASS
```

---

## 1b. Tool-level (trajectory) assertions — `cxas test-tools`

Because the golden tool-trajectory metric can't reference toolset op IDs (above), the goldens
assert behaviour via the LLM judge but don't *deterministically* assert "`getPayoffQuote` ran
with `loan_id=TL-1001`." We close that gap with **`cxas test-tools`**
([`agent/evals/tool_tests.yaml`](../agent/evals/tool_tests.yaml)), which **invokes each toolset
operation directly** (no model, no quota) and asserts the exact response:

- `getAccountSummary(loan_id=TL-1001, phone_last4=4417)` → `emi_amount=8980`, `outstanding=181240`
- `getPayoffQuote(loan_id=TL-1001, …)` → `foreclosure_charge=3625`, `accrued_interest=2492`, **`total_payoff_amount=187357`**
- `createTicket(…, category=complaint)` → `status=open`, ticket id + SLA present

**Result: PASS (3/3).** These are concrete trajectory/contract assertions on the exact ops and
args — they verify the *math*, not just that a transcript "looks right."

```bash
.venv-scrapi/bin/cxas test-tools --app-name "$APP" --file agent/evals/tool_tests.yaml
```

## 1c. Unit tests — `make test` (pytest)

The trickiest pure logic has direct unit tests ([`tests/test_backend.py`](../tests/test_backend.py),
20 cases, fully offline): the **`inr()`** Indian digit-grouping (lakh/crore, negatives), the
**foreclosure/payoff math** (2% + accrued → ₹1,87,357), the **identity gate** (403 on wrong/
missing last-4), and 404/401 paths. Run with `make test`.

## Feedback for the CES eval team
The golden `toolCall` expectation only accepts the top-level `apps/<app>/tools/<tool_id>`
template, so a golden **cannot reference an OpenAPI-**toolset** operation** (e.g.
`.../toolsets/servicing/tools/getPayoffQuote`) — the push is rejected with a ResourceName
template error. Net effect: golden tool-trajectory checking is unavailable for any agent that
wires REST tools as an OpenAPI toolset (the recommended pattern). Suggested fix: allow toolset
operation IDs in golden `toolCall` references (or a `toolset`+`toolId` pair). Until then, we
use `cxas test-tools` for deterministic tool-level assertions (§1b).

---

## 2. Lint (`cxas lint`)

`cxas lint` runs 60+ structural / best-practice rules over a pulled app directory.

```bash
APP=projects/gcex-pilot-16862/locations/us/apps/tilicho-credit-scrapi
.venv-scrapi/bin/cxas pull "$APP" --target-dir agent/scrapi_app \
    --project-id gcex-pilot-16862 --location us
.venv-scrapi/bin/cxas lint --app-dir agent/scrapi_app
```

**Result: 11 errors → 2 errors.** Everything legitimately fixable is fixed; the 2 remaining
are confirmed **bugs in `cxas-scrapi` itself** (verified against the rule source).

### Fixed (9 errors)
- **I001 ×9** — instructions were prose. Restructured all 3 agents into CES best-practice
  XML: `<role>` / `<persona>` / `<taskflow>` with `<step>` children (also satisfies **I002**).
- **T012 (the file-search tool description)** — added a description to the `policy_kb` tool.

### Remaining (2 errors — upstream `cxas-scrapi` bugs)
- **`A006` rootAgent not found.** SCRAPI's own `pull` writes `rootAgent: "Credit Assist"`
  (display name, with a space) but names the agent directory `Credit_Assist` (space→
  underscore). The rule then does `agents/<rootAgent>` and can't find `agents/Credit Assist`.
  The exported config is self-inconsistent — the rootAgent **is** valid in the live app.
- **`T012` on the RAG tool.** The rule only inspects `pythonFunction.description` and
  `widgetTool.description`. Our `policy_kb` is a `fileSearchTool`, which the rule doesn't
  recognise, so it reports "missing description" even though the description **is present**
  (see `agent/scrapi_app/.../tools/policy_kb/policy_kb.json`). Any file-search tool trips
  this rule.

Neither is fixable without removing the RAG tool or uglifying display names to dodge a
linter bug — so we keep the correct config and document the false-positives.

### Warnings (3 × `I012`, non-failing)
"Agent lists tool `policy_kb` but instruction never references it." The instructions **do**
mention the `policy_kb` tool in plain English, but the rule only counts CES's
`{@TOOL: policy_kb}` template syntax. We don't inject that token because it can alter runtime
prompt rendering for a cosmetic warning. Left as-is intentionally.

---

## Takeaway
- **Behavior (goldens):** 6/6 scenarios **PASS** in the console and CLI (account lookups,
  grounded policy, session memory, hardship handling, compliance guardrail).
- **Tool trajectory (`cxas test-tools`):** 3/3 — deterministic assertions on the exact toolset
  ops + args + response (incl. the payoff math), covering what goldens can't express.
- **Unit tests (`make test`):** 20 pytest cases on `inr()` + payoff math + the verification gate.
- **Structure (lint):** 11 → 2; the 2 residual errors + 3 warnings are documented upstream
  `cxas-scrapi` limitations, with source-level evidence.
- The golden toolset-op-ID gap is surfaced as **feedback for the CES eval team** (above).
- All reproducible from this repo against the SCRAPI-built app.
