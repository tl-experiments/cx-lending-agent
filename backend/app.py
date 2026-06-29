"""
Tilicho Credit Assist — mock lending backend.

A small, dependency-light FastAPI service that the CX agent's OpenAPI tools call.
All data is SYNTHETIC and PII-safe. Endpoints model a digital lender's servicing
APIs: account lookup, payoff/foreclosure quote, and ticket creation.

Run locally:
    pip install -r requirements.txt
    uvicorn app:app --reload --port 8080
Docs/OpenAPI:
    http://localhost:8080/docs   (Swagger UI)
    http://localhost:8080/openapi.json
"""
import base64
import calendar
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(
    title="Tilicho Credit Servicing API",
    version="1.0.0",
    description="Synthetic loan-servicing API for the GCEX POC. PII-safe demo data.",
    servers=[{"url": "http://localhost:8080", "description": "Local mock"}],
)

# CORS limited to our own surfaces: the Cloud Run origin, the branded subdomain, and any
# *.workers.dev. Not a wildcard. (The UIs fetch same-origin relative paths, so this is belt-and-braces.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://tilicho-credit-api-804472053350.us-central1.run.app",
                   "https://credit-assist.gecx.tilicho.in"],
    allow_origin_regex=r"https://[a-z0-9-]+\.workers\.dev",
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"],
)

# Static assets (Tour screenshots) served from backend/static/ (ships with the deploy).
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(_STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# --- Shared-secret auth for the OpenAPI tool. In production this is mounted from Secret
# Manager (BACKEND_API_KEY env); "demo-key-gcex" is only a local-dev fallback, NOT the
# real secret value (which is rotated and never committed). ---
API_KEY = os.environ.get("BACKEND_API_KEY", "demo-key-gcex")


def _check_key(api_key: Optional[str]) -> None:
    # Key is required. The CES OpenAPI tool sends it via apiKeyConfig backed by a
    # Secret Manager secret version (see infra/setup-secret.sh).
    if not api_key or not hmac.compare_digest(api_key, API_KEY):  # constant-time
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def _verify(rec: dict, phone_last4: Optional[str]) -> None:
    """Identity gate: 403 unless the supplied last-4 matches the loan's registered phone.
    This is the real check behind 'verify by loan ID + last-4 phone' — enforced server-side
    so account data is never returned without a matching phone, regardless of agent behaviour."""
    if not phone_last4 or str(phone_last4).strip() != rec["phone_last4"]:
        raise HTTPException(
            status_code=403,
            detail="identity verification failed: loan ID and last-4 phone do not match",
        )


# --------------------------- Synthetic data ---------------------------
# loan_id -> record. Phone last-4 lets the agent verify identity in a demo.
LOANS = {
    "TL-1001": {
        "loan_id": "TL-1001",
        "borrower_name": "Asha R.",
        "phone_last4": "4417",
        "product": "Personal Loan",
        "principal": 250000,
        "outstanding_principal": 181240,
        "interest_rate_apr": 16.5,
        "emi_amount": 8980,
        "emi_day_of_month": 5,
        "tenure_months": 36,
        "emis_paid": 9,
        "status": "active",
        "days_past_due": 0,
    },
    "TL-1002": {
        "loan_id": "TL-1002",
        "borrower_name": "Vikram S.",
        "phone_last4": "9023",
        "product": "Two-Wheeler Loan",
        "principal": 120000,
        "outstanding_principal": 64500,
        "interest_rate_apr": 14.0,
        "emi_amount": 4150,
        "emi_day_of_month": 12,
        "tenure_months": 30,
        "emis_paid": 14,
        "status": "active",
        "days_past_due": 0,
    },
    "TL-1003": {
        "loan_id": "TL-1003",
        "borrower_name": "Meera P.",
        "phone_last4": "1188",
        "product": "Personal Loan",
        "principal": 400000,
        "outstanding_principal": 372100,
        "interest_rate_apr": 18.0,
        "emi_amount": 14460,
        "emi_day_of_month": 28,
        "tenure_months": 36,
        "emis_paid": 2,
        "status": "delinquent",
        "days_past_due": 22,
    },
}

TICKETS: dict[str, dict] = {}
_ticket_seq = 5000


def _next_due_date(day_of_month: int, today: Optional[date] = None) -> date:
    today = today or date.today()
    year, month = today.year, today.month
    if today.day >= day_of_month:
        month += 1
        if month > 12:
            month, year = 1, year + 1
    # clamp to the target month's last day (e.g. the 31st in February → 28/29), not a flat 28
    day = min(day_of_month, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def inr(n) -> str:
    """Format an integer rupee amount with the ₹ symbol and Indian digit grouping.
    e.g. 181240 -> '₹1,81,240', 8980 -> '₹8,980'."""
    n = int(n)
    s = str(abs(n))
    if len(s) > 3:
        last3, other, parts = s[-3:], s[:-3], []
        while len(other) > 2:
            parts.insert(0, other[-2:]); other = other[:-2]
        if other:
            parts.insert(0, other)
        grouped = ",".join(parts) + "," + last3
    else:
        grouped = s
    return ("-" if n < 0 else "") + "₹" + grouped


# --------------------------- Schemas ---------------------------
class AccountSummary(BaseModel):
    loan_id: str
    borrower_name: str
    product: str
    status: str
    outstanding_principal: int = Field(description="Current principal owed, in INR")
    outstanding_principal_display: str = Field(description="Outstanding principal, formatted e.g. ₹1,81,240")
    emi_amount: int = Field(description="Monthly instalment, in INR")
    emi_amount_display: str = Field(description="EMI, formatted e.g. ₹8,980")
    next_due_date: str = Field(description="Next EMI due date, YYYY-MM-DD")
    interest_rate_apr: float
    emis_paid: int
    tenure_months: int
    days_past_due: int


class PayoffQuote(BaseModel):
    loan_id: str
    outstanding_principal: int
    outstanding_principal_display: str
    foreclosure_charge: int = Field(description="Charge to close early, in INR")
    foreclosure_charge_display: str
    accrued_interest: int
    accrued_interest_display: str
    total_payoff_amount: int = Field(description="Total to fully close the loan today, INR")
    total_payoff_amount_display: str = Field(description="Total payoff, formatted e.g. ₹1,87,357")
    quote_valid_until: str
    notes: str


class TicketRequest(BaseModel):
    loan_id: str
    phone_last4: str = Field(default="", description="Last 4 digits of the registered phone, for identity verification")
    category: str = Field(description="e.g. restructuring, complaint, kyc_update, callback")
    message: str


class TicketResponse(BaseModel):
    ticket_id: str
    loan_id: str
    category: str
    status: str
    sla_resolution_by: str


# --------------------------- Endpoints ---------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/accounts/{loan_id}", response_model=AccountSummary, operation_id="getAccountSummary")
def get_account(loan_id: str, phone_last4: str = "", x_api_key: Optional[str] = Header(default=None)):
    """Look up a borrower's loan account: balance, EMI, next due date, status.
    Requires phone_last4 matching the loan's registered phone (identity verification)."""
    _check_key(x_api_key)
    rec = LOANS.get(loan_id.upper())
    if not rec:
        raise HTTPException(status_code=404, detail=f"no loan found for {loan_id}")
    _verify(rec, phone_last4)
    return AccountSummary(
        next_due_date=_next_due_date(rec["emi_day_of_month"]).isoformat(),
        outstanding_principal_display=inr(rec["outstanding_principal"]),
        emi_amount_display=inr(rec["emi_amount"]),
        **{k: rec[k] for k in (
            "loan_id", "borrower_name", "product", "status", "outstanding_principal",
            "emi_amount", "interest_rate_apr", "emis_paid", "tenure_months", "days_past_due",
        )},
    )


@app.get("/accounts/{loan_id}/payoff", response_model=PayoffQuote, operation_id="getPayoffQuote")
def get_payoff(loan_id: str, phone_last4: str = "", x_api_key: Optional[str] = Header(default=None)):
    """Compute a foreclosure / early-payoff quote for a loan.
    Requires phone_last4 matching the loan's registered phone (identity verification)."""
    _check_key(x_api_key)
    rec = LOANS.get(loan_id.upper())
    if not rec:
        raise HTTPException(status_code=404, detail=f"no loan found for {loan_id}")
    _verify(rec, phone_last4)
    op = rec["outstanding_principal"]
    # Synthetic, transparent math: 2% foreclosure charge + ~1 month accrued interest.
    foreclosure_charge = round(op * 0.02)
    accrued_interest = round(op * rec["interest_rate_apr"] / 100 / 12)
    total = op + foreclosure_charge + accrued_interest
    return PayoffQuote(
        loan_id=rec["loan_id"],
        outstanding_principal=op,
        outstanding_principal_display=inr(op),
        foreclosure_charge=foreclosure_charge,
        foreclosure_charge_display=inr(foreclosure_charge),
        accrued_interest=accrued_interest,
        accrued_interest_display=inr(accrued_interest),
        total_payoff_amount=total,
        total_payoff_amount_display=inr(total),
        quote_valid_until=(date.today() + timedelta(days=7)).isoformat(),
        notes="Foreclosure charge 2% of principal per loan T&Cs. Quote valid 7 days.",
    )


@app.post("/tickets", response_model=TicketResponse, operation_id="createTicket")
def create_ticket(req: TicketRequest, x_api_key: Optional[str] = Header(default=None)):
    """Raise a servicing ticket: restructuring, complaint, KYC update, or callback."""
    _check_key(x_api_key)
    rec = LOANS.get(req.loan_id.upper())
    if not rec:
        raise HTTPException(status_code=404, detail=f"no loan found for {req.loan_id}")
    _verify(rec, req.phone_last4)
    global _ticket_seq
    _ticket_seq += 1
    tid = f"TKT-{_ticket_seq}"
    # Complaints get a tighter SLA to showcase the fair-practice / grievance angle.
    sla_days = 3 if req.category == "complaint" else 5
    rec = {
        "ticket_id": tid,
        "loan_id": req.loan_id.upper(),
        "category": req.category,
        "status": "open",
        "sla_resolution_by": (date.today() + timedelta(days=sla_days)).isoformat(),
    }
    TICKETS[tid] = rec
    return TicketResponse(**rec)


# =============================================================================
# Web chat demo: a /chat proxy that relays to the CES agent (runSession), plus a
# served chat UI at /ui. Server-side auth uses ADC (the Cloud Run runtime SA, scoped to
# ces.client). Gives a clickable, shareable demo without exposing credentials to the browser.
# =============================================================================
CES_APP = os.environ.get(
    "CES_APP", "projects/gcex-pilot-16862/locations/us/apps/tilicho-credit-scrapi"
)
_ces_creds = None


def _ces_token():
    global _ces_creds
    import google.auth
    from google.auth.transport.requests import Request as GAuthRequest
    if _ces_creds is None:
        _ces_creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _ces_creds.valid:
        _ces_creds.refresh(GAuthRequest())
    return _ces_creds.token


AGENT_FULL = f"{CES_APP}/agents/credit-assist"
AGENT_LEAN = f"{CES_APP}/agents/credit-assist-lean"


class ChatRequest(BaseModel):
    message: str
    session_id: str = "web-demo"
    mode: str = "full"  # "full" (rich; auto-falls back to lean on 429) | "lean"


# Map raw tool identifiers → friendly transparency badges shown in the UI.
_TOOL_LABELS = {
    "getAccountSummary": "Account lookup",
    "getPayoffQuote": "Payoff quote",
    "createTicket": "Ticket raised",
}


def _signals(d):
    """Which tools ran + whether the answer was grounded — DERIVED from the run trace
    (substring scan of the runSession response), not authoritative platform telemetry.
    Used only for the illustrative UI badges."""
    blob = json.dumps(d)
    tools = [label for raw, label in _TOOL_LABELS.items() if raw in blob]
    grounded = ("policy_kb" in blob) or ("FileSearchTool" in blob) or ("fileSearch" in blob)
    return tools, grounded


def _run_session(session_id, message, entry_agent):
    """One runSession against a specific entry agent, with retry/backoff.
    Returns a dict: either {reply, tools, grounded, latency_ms} or
    {reply:None, rate_limited, error}."""
    url = f"https://ces.googleapis.com/v1beta/{CES_APP}/sessions/{session_id}:runSession"
    cfg = {"entryAgent": entry_agent} if entry_agent else {}
    body = json.dumps({"config": cfg, "inputs": [{"text": message}]}).encode()
    last = None
    for attempt in range(7):
        r = urllib.request.Request(url, data=body, method="POST")
        r.add_header("Authorization", f"Bearer {_ces_token()}")
        r.add_header("Content-Type", "application/json")
        try:
            t0 = time.time()
            with urllib.request.urlopen(r) as resp:
                d = json.loads(resp.read().decode())
            latency_ms = int((time.time() - t0) * 1000)  # authoritative round-trip time
            texts = [o.get("text", "") for o in d.get("outputs", []) if o.get("text")]
            tools, grounded = _signals(d)
            return {"reply": "\n".join(t for t in texts if t) or "(no response)",
                    "tools": tools, "grounded": grounded, "latency_ms": latency_ms}
        except urllib.error.HTTPError as e:
            last = e.read().decode()[:300]
            # Ride out transient platform errors: 429 rate limits AND the documented CES
            # runSession 400/5xx "settling" window (~10-60s after any agent patch, or brief
            # flakiness on the free-trial app). Escalating backoff, ~24s total budget — far
            # under Cloud Run's request timeout. Only runs on failure: the success path is
            # untouched and still returns in ~2s.
            if (e.code == 429 or e.code == 400 or 500 <= e.code < 600) and attempt < 6:
                time.sleep(min(2 + attempt, 5))   # 2,3,4,5,5,5 -> ~24s
            else:
                break
    rl = bool(last and ("429" in last or "exhaust" in last.lower() or "RESOURCE_EXHAUSTED" in last))
    return {"reply": None, "rate_limited": rl, "error": last or "request failed"}


# Per-session memory of the verified loan, so the agent never re-asks for identity
# mid-conversation (CES chat history alone proved unreliable across topic switches).
@app.post("/chat")
def chat(req: ChatRequest):
    """Relay one user turn to the CES agent. Session continuity — remembering the verified
    loan + last-4 across turns — is handled by **CES session memory** (keyed on session_id);
    we do NOT re-implement memory or inject identity context. Identity is still enforced
    server-side: each servicing tool requires phone_last4 and returns 403 on mismatch.
    mode='lean' uses the token-minimised agent; 'full' auto-falls back to lean on a rate limit."""
    mode = (req.mode or "full").lower()
    entry = AGENT_LEAN if mode == "lean" else AGENT_FULL
    res = _run_session(req.session_id, req.message, entry)
    if res.get("reply") is None and res.get("rate_limited") and mode != "lean":
        # graceful degradation: retry the same turn on the lean (cheaper) agent
        alt = _run_session(req.session_id, req.message, AGENT_LEAN)
        if alt.get("reply"):
            alt["lean_fallback"] = True
            return alt
    return res


_CHAT_UI = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tilicho Credit Assist</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='112' fill='%23E00917'/><g transform='translate(256 256) scale(1.15) translate(-150 -132)' fill='%23ffffff'><path d='M2 4 H128 V260 H78 V72 Q78 50 56 50 H2 Z'/><path d='M2 4 H128 V260 H78 V72 Q78 50 56 50 H2 Z' transform='rotate(180 150 132)'/></g></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
 --ink:#0A0E16;--ink2:#0F141E;--ink3:#161D29;--inkline:rgba(255,255,255,.09);
 --red:#E00917;--green:#39B98A;--txhi:#EBEEF3;--txlo:#8A96A6;--txmute:#5E6A7A;
 --disp:'Space Grotesk',sans-serif;--body:'IBM Plex Sans',sans-serif;--mono:'IBM Plex Mono',monospace;
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;font-family:var(--body);color:var(--txlo);background:var(--ink);
 -webkit-font-smoothing:antialiased}
.app{max-width:860px;margin:0 auto;height:100dvh;display:flex;flex-direction:column;background:var(--ink);
 border-left:1px solid var(--inkline);border-right:1px solid var(--inkline)}
.mono{font-family:var(--mono);text-transform:uppercase;letter-spacing:.16em}
/* header */
.hd{display:flex;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--inkline)}
.logo{display:flex;align-items:center;gap:9px;text-decoration:none}
.logo .wm{font-family:var(--disp);font-weight:600;font-size:16px;letter-spacing:-.01em;color:var(--txhi)}
.logo .wm b{font-weight:600}.logo .wm span{color:var(--txmute)}
.vsep{width:1px;height:26px;background:var(--inkline)}
.ttl .k{font-family:var(--mono);font-size:10px;letter-spacing:.22em;color:var(--txmute);text-transform:uppercase}
.ttl h1{font-family:var(--disp);font-size:15px;font-weight:600;margin:2px 0 0;color:var(--txhi);letter-spacing:-.01em}
.nav{margin-left:auto;display:flex;align-items:center;gap:18px}
.nav a{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--txlo);text-decoration:none;display:inline-flex;align-items:center;height:28px;line-height:1}
.nav a:hover{color:var(--red)}
.lean{display:inline-flex;align-items:center;gap:7px;height:28px;line-height:1;font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--txlo);cursor:pointer;user-select:none}
.lean input{accent-color:var(--red);cursor:pointer;margin:0;width:13px;height:13px}
.about{cursor:pointer;border:1px solid var(--inkline);background:none;color:var(--txlo);width:28px;height:28px;font-size:12px;font-family:var(--mono);display:inline-flex;align-items:center;justify-content:center;padding:0}
.about:hover{border-color:var(--red);color:var(--red)}
/* info */
.info{display:none;padding:14px 22px;border-bottom:1px solid var(--inkline);font-size:13px;color:var(--txlo);line-height:1.6;background:var(--ink2)}
.info.open{display:block}.info b{color:var(--txhi);font-weight:500}
/* log */
.log{flex:1;overflow-y:auto;padding:26px 22px;display:flex;flex-direction:column;gap:20px;scroll-behavior:smooth}
.row{display:flex;gap:12px;align-items:flex-start;max-width:86%;animation:rise .26s ease}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.row.user{align-self:flex-end;flex-direction:row-reverse}
.av{width:28px;height:28px;flex:0 0 28px;display:grid;place-items:center;border:1px solid var(--inkline);background:var(--ink2)}
.av.user{font-family:var(--mono);font-size:9px;letter-spacing:.1em;color:var(--txmute)}
.bubble{padding:12px 16px;line-height:1.6;font-size:14.5px;border:1px solid var(--inkline);background:var(--ink2);color:var(--txhi)}
.user .bubble{background:var(--ink3)}
.bot .bubble.err{border-color:var(--red);color:#f3b6ba}
.meta{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}
.chip{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;padding:3px 8px;border:1px solid var(--inkline);color:var(--txmute)}
.chip.ground{color:var(--green);border-color:rgba(57,185,138,.35)}
.time{font-family:var(--mono);font-size:9.5px;letter-spacing:.1em;color:var(--txmute);margin-top:7px}
/* typing */
.typing{display:flex;gap:5px;padding:3px 1px}
.typing i{width:6px;height:6px;border-radius:50%;background:var(--txmute);animation:bnc 1.2s infinite}
.typing i:nth-child(2){animation-delay:.15s}.typing i:nth-child(3){animation-delay:.3s}
@keyframes bnc{0%,60%,100%{opacity:.35}30%{opacity:1}}
/* welcome (left-aligned per brand) */
.welcome{padding:6px 2px}
.welcome .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.22em;text-transform:uppercase;color:var(--red)}
.welcome h2{font-family:var(--disp);font-weight:600;color:var(--txhi);font-size:26px;letter-spacing:-.02em;margin:12px 0 8px;max-width:18ch}
.welcome p{font-size:15px;color:var(--txlo);max-width:52ch;margin:0;line-height:1.6}
.sugg{display:flex;flex-wrap:wrap;gap:9px;margin-top:20px}
.sg{cursor:pointer;background:none;border:1px solid var(--inkline);padding:10px 13px;font-family:var(--mono);font-size:11px;
 letter-spacing:.06em;color:var(--txlo);position:relative;transition:.14s}
.sg:hover{color:var(--txhi);border-color:var(--txmute);box-shadow:inset 2px 0 0 var(--red)}
.bar{display:flex;gap:8px;padding:12px 18px 0;flex-wrap:wrap}
.bar .sg{padding:8px 11px;font-size:10.5px}
.barlbl{font-family:var(--mono);font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:var(--txmute);align-self:center;margin-right:2px}
/* composer */
.composer{display:flex;gap:10px;padding:14px 18px;border-top:1px solid var(--inkline);align-items:center}
.composer input{flex:1;padding:13px 6px;border:0;border-bottom:1px solid var(--inkline);background:none;color:var(--txhi);
 font-size:15px;outline:none;font-family:inherit}
.composer input:focus{border-bottom-color:var(--red)}
.composer input::placeholder{color:var(--txmute)}
.send{width:44px;height:44px;border:0;background:var(--red);color:#fff;cursor:pointer;font-size:17px;display:grid;place-items:center;transition:.14s}
.send:hover{background:#c00813}.send:disabled{opacity:.4;cursor:default}
.spk{margin-top:9px;background:none;border:1px solid var(--inkline);font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;
 text-transform:uppercase;padding:4px 9px;cursor:pointer;color:var(--txmute)}
.spk:hover{border-color:var(--txmute);color:var(--txlo)}
.mic{background:none;color:var(--txlo);border:1px solid var(--inkline)}
.mic:hover{border-color:var(--red);color:var(--red)}
.mic.rec{background:var(--red);color:#fff;border-color:var(--red)}
.foot{font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--txmute);padding:0 22px 12px}
</style></head><body>
<div class="app">
 <div class="hd">
  <a class="logo" href="https://tilicho.in" target="_blank" title="Tilicho Labs">
   <img src="/static/img/tilicho-logo.webp" alt="Tilicho Labs" style="height:26px;width:auto;display:block"></a>
  <div class="vsep"></div>
  <div class="ttl"><div class="k">Customer experience · demo</div><h1>Credit Assist</h1></div>
  <div class="nav">
   <label class="lean" title="Lean mode: fewer tokens per turn. Off = full richness."><input type="checkbox" id="lean"> Lean</label>
   <a href="/">Tour ↗</a>
   <a href="/insights">Insights ↗</a>
   <button class="about" id="ab" title="About this demo">i</button>
  </div>
 </div>
 <div class="info" id="info">
  <b>Live POC.</b> A lending support agent built code-first on Gemini Enterprise for Customer
  Experience — real tool calls (account, payoff, tickets), policy answers grounded in a
  knowledge base, compliant by design. The mono tags under each reply show when a tool ran or
  the answer was grounded. All data is synthetic.
 </div>
 <div class="log" id="log">
  <div class="welcome" id="welcome">
   <div class="k">Gemini Enterprise for CX</div>
   <h2>Lending support, answered.</h2>
   <p>Ask about your loan — EMI, foreclosure, payments, KYC, or policy. Real tool calls and grounded answers.</p>
  </div>
 </div>
 <div class="bar" id="bar"></div>
 <form class="composer" id="f">
  <button type="button" class="send mic" id="mic" title="Speak (voice input)">&#127908;</button>
  <input id="i" autocomplete="off" placeholder="Ask about your loan…" autofocus>
  <button class="send" id="b" title="Send">→</button>
 </form>
</div>
<script>
const log=document.getElementById('log'),inp=document.getElementById('i'),btn=document.getElementById('b'),
 f=document.getElementById('f'),welcome=document.getElementById('welcome');
const sid='web-'+Math.random().toString(36).slice(2,9);
document.getElementById('ab').onclick=()=>document.getElementById('info').classList.toggle('open');

// Openers carry a loan id (no session yet). Follow-ups omit it — the session
// remembers the loan — so they read as the SAME customer continuing the thread.
const OPENERS=[
 ["Check my EMI","My loan is TL-1001, phone 4417. What's my EMI and balance?"],
 ["Foreclosure quote","I want to foreclose loan TL-1001, phone 4417. What's the payoff?"],
 ["Late payment fee","What is the late payment fee if I miss an EMI?"],
 ["Hardship help","I lost my job and can't pay. Loan TL-1003, phone 1188."],
 ["Update KYC","How do I update my KYC? Loan TL-1002, phone 9023."]];
const FU={
 acct:[["When's it due?","When is my next EMI due?"],["Foreclosure quote","What's the payoff to foreclose this loan?"],["If I miss a payment?","What happens if I miss a payment?"]],
 payoff:[["Talk to a specialist","Can you connect me to a specialist to foreclose?"],["Part-prepay instead?","Can I make a part-prepayment instead?"],["My balance?","What's my outstanding balance?"]],
 ticket:[["Anything else?","What else can you help me with on this loan?"],["My balance?","What's my outstanding balance?"],["Talk to a human","I'd like to speak to a human agent."]],
 policy:[["Check my EMI","What's my EMI?"],["Foreclosure charge","What's the foreclosure charge?"],["Raise a complaint","I'd like to raise a complaint."]]};
const GENERIC=[["Check my EMI","What's my EMI?"],["Foreclosure quote","What's the payoff to foreclose my loan?"],["Talk to a human","I'd like to speak to a human agent."]];
const bar=document.getElementById('bar');
function renderSug(list){bar.innerHTML='<span class="barlbl">Try</span>';(list||GENERIC).forEach(([label,text])=>{
 const b=document.createElement('button');b.type='button';b.className='sg';b.textContent=label;
 b.onclick=()=>{inp.value=text;send();};bar.appendChild(b);});}
function nextSug(j){const t=j.tools||[];
 if(t.includes('Payoff quote'))return FU.payoff;
 if(t.includes('Ticket raised'))return FU.ticket;
 if(t.includes('Account lookup'))return FU.acct;
 if(j.grounded)return FU.policy;
 return GENERIC;}
renderSug(OPENERS);
const CHEV='<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="#E00917" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 5l6 7-6 7"/><path d="M12 5l6 7-6 7"/></svg>';

function esc(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmt(t){return esc(t).replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>')
 .replace(/^\\s*[-*]\\s+(.*)$/gm,'• $1').replace(/\\n/g,'<br>');}
function now(){return new Date().toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}

function row(who){const r=document.createElement('div');r.className='row '+who;
 const a=document.createElement('div');a.className='av '+who;if(who==='bot'){a.innerHTML=CHEV;}else{a.textContent='YOU';}
 const col=document.createElement('div');const bub=document.createElement('div');bub.className='bubble';
 col.appendChild(bub);r.appendChild(a);r.appendChild(col);log.appendChild(r);
 log.scrollTop=log.scrollHeight;return {bub,col};}

function addUser(t){if(welcome)welcome.style.display='none';const {bub,col}=row('user');bub.textContent=t;
 const ti=document.createElement('div');ti.className='time';ti.textContent=now();ti.style.textAlign='right';col.appendChild(ti);}

function typing(){const {bub}=row('bot');bub.innerHTML='<div class="typing"><i></i><i></i><i></i></div>';return bub;}

function badges(col,j,leanUsed){const meta=document.createElement('div');meta.className='meta';
 const add=(txt,cls)=>{const c=document.createElement('span');c.className='chip'+(cls?' '+cls:'');c.textContent=txt;meta.appendChild(c);};
 if(leanUsed||j.lean_fallback)add('Lean'+(j.lean_fallback?' · auto':''));
 (j.tools||[]).forEach(t=>add(t));
 if(j.grounded)add('Grounded','ground');
 if(j.latency_ms)add((j.latency_ms/1000).toFixed(1)+'s');
 if(meta.children.length)col.appendChild(meta);
 const ti=document.createElement('div');ti.className='time';ti.textContent=now();col.appendChild(ti);}

async function send(){const m=inp.value.trim();if(!m||btn.disabled)return;addUser(m);inp.value='';btn.disabled=true;
 const bub=typing();const col=bub.parentElement;
 const slow=setTimeout(()=>{bub.innerHTML='<span style="color:#8a93a0">Working… free-tier rate limit may slow this</span>';},3500);
 const ctrl=new AbortController();const killer=setTimeout(()=>ctrl.abort(),60000);
 const lean=document.getElementById('lean').checked;
 try{const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({message:m,session_id:sid,mode:lean?'lean':'full'}),signal:ctrl.signal});
  const j=await r.json();clearTimeout(slow);
  if(j.reply){bub.innerHTML=fmt(j.reply);badges(col,j,lean);addSpeak(col,j.reply);renderSug(nextSug(j));}
  else if(j.rate_limited){bub.className='bubble err';bub.textContent='Rate limited — wait ~30s and try again.';}
  else{bub.className='bubble err';bub.textContent='Could not reach the agent. Try again shortly.';}}
 catch(err){clearTimeout(slow);bub.className='bubble err';
  bub.textContent=(err.name==='AbortError')?'Timed out — wait ~30s and retry.':'Network error. Try again.';}
 finally{clearTimeout(killer);btn.disabled=false;inp.focus();log.scrollTop=log.scrollHeight;}}
f.onsubmit=(e)=>{e.preventDefault();send();};
// Voice (Pillar 2): HD playback of replies + browser speech-to-text input
let curAudio=null;
function speak(text,btn){if(curAudio)curAudio.pause();btn.textContent='…';
 fetch('/tts?text='+encodeURIComponent(text.slice(0,1200))).then(r=>r.blob()).then(b=>{
  curAudio=new Audio(URL.createObjectURL(b));curAudio.play();btn.textContent='♪ Listen';}).catch(()=>{btn.textContent='♪ Listen';});}
function addSpeak(col,text){const b=document.createElement('button');b.className='spk';b.textContent='♪ Listen';b.onclick=()=>speak(text,b);col.appendChild(b);}
const mic=document.getElementById('mic');const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
if(SR&&mic){const rec=new SR();rec.lang='en-IN';rec.interimResults=false;
 rec.onresult=e=>{inp.value=e.results[0][0].transcript;send();};
 rec.onend=()=>mic.classList.remove('rec');rec.onerror=()=>mic.classList.remove('rec');
 mic.onclick=()=>{mic.classList.add('rec');try{rec.start();}catch(e){mic.classList.remove('rec');}};}
else if(mic){mic.style.display='none';}
</script></body></html>"""


@app.get("/agent", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
def chat_ui():
    return _CHAT_UI


# =============================================================================
# Conversational Insights dashboard — reads the CES BigQuery export (no agent
# quota needed). /insights/data runs the analytics; /insights renders them.
# Data hygiene: the SQL excludes the rep-console's own sessions (conversation_id
# "assist%", which feed an escalated transcript back in) and strips the legacy
# "[Context: …]" identity hint that older turns appended — so the dashboard shows
# only genuine borrower utterances, not the app's own scaffolding.
# =============================================================================
PROJECT_ID = os.environ.get("PROJECT_ID", "gcex-pilot-16862")
BQ_TABLE = os.environ.get("BQ_TABLE", "gcex-pilot-16862.tilicho_cx_insights.tilicho-credit-scrapi")

_CALL_DRIVERS_SQL = """
WITH user_msgs AS (
  -- Aggregate ALL of a conversation's user text into one string, so each conversation is
  -- classified ONCE (one primary driver by the CASE priority) — the per-driver counts then
  -- sum to the distinct conversation count rather than double-counting multi-topic chats.
  SELECT conversation_id,
    LOWER(STRING_AGG(
      (SELECT STRING_AGG(REGEXP_REPLACE(JSON_VALUE(c,"$.text"), r"\\s*\\[Context:[\\s\\S]*", "")," ")
       FROM UNNEST(JSON_QUERY_ARRAY(m.chunks)) c
       WHERE JSON_VALUE(c,"$.text") IS NOT NULL), " ")) AS q
  FROM `{{TABLE}}`, UNNEST(messages) m
  WHERE m.role = "user" AND conversation_id NOT LIKE "assist%"
  GROUP BY conversation_id),
classified AS (
  SELECT conversation_id, CASE
    WHEN REGEXP_CONTAINS(q, r"foreclos|payoff") THEN "Foreclosure"
    WHEN REGEXP_CONTAINS(q, r"kyc") THEN "KYC update"
    WHEN REGEXP_CONTAINS(q, r"complain|wrongly|frustrat|unhappy") THEN "Complaint"
    WHEN REGEXP_CONTAINS(q, r"lost.*job|can.?.?t pay|hardship|restructur") THEN "Hardship"
    WHEN REGEXP_CONTAINS(q, r"fee|charge|prepay|policy|penalty") THEN "Policy/Fees"
    WHEN REGEXP_CONTAINS(q, r"emi|balance|due|outstanding") THEN "Account/EMI"
    ELSE "Other" END AS call_driver
  FROM user_msgs WHERE q IS NOT NULL)
SELECT call_driver, COUNT(DISTINCT conversation_id) AS conversations,
  ROUND(100*COUNT(DISTINCT conversation_id)/SUM(COUNT(DISTINCT conversation_id)) OVER (),1) AS pct
FROM classified GROUP BY call_driver ORDER BY conversations DESC
"""

_TRANSCRIPTS_SQL = """
WITH turns AS (
  SELECT conversation_id, m.role AS role, m.event_time AS t,
    (SELECT STRING_AGG(REGEXP_REPLACE(JSON_VALUE(c,"$.text"), r"\\s*\\[Context:[\\s\\S]*", "")," ")
     FROM UNNEST(JSON_QUERY_ARRAY(m.chunks)) c WHERE JSON_VALUE(c,"$.text") IS NOT NULL) AS text
  FROM `{{TABLE}}`, UNNEST(messages) m
  WHERE conversation_id NOT LIKE "assist%")
SELECT conversation_id,
  STRING_AGG(IF(role="user", text, NULL), " ") AS borrower_said,
  STRING_AGG(IF(role!="user", text, NULL), " ") AS agent_replied
FROM turns WHERE text IS NOT NULL AND TRIM(text) != ""
GROUP BY conversation_id ORDER BY conversation_id
"""


def _bq_query(sql):
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT_ID}/queries"
    body = json.dumps({"query": sql.replace("{{TABLE}}", BQ_TABLE),
                       "useLegacySql": False, "timeoutMs": 30000}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {_ces_token()}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        d = json.loads(resp.read().decode())
    fields = [f["name"] for f in d.get("schema", {}).get("fields", [])]
    rows = []
    for r in d.get("rows", []):
        rows.append({fields[i]: cell.get("v") for i, cell in enumerate(r.get("f", []))})
    return rows


# Real Contact Center AI Insights (CCAI) analysis, pulled into BigQuery by insights/cci_analyze.py
# (per-conversation Google ML sentiment + entities). This is CCAI's own analysis, not ours.
BQ_INSIGHTS_TABLE = os.environ.get(
    "BQ_INSIGHTS_TABLE", "gcex-pilot-16862.tilicho_cx_insights.cci_insights")


def _sani(cid):
    return re.sub(r"[^a-z0-9-]", "-", (cid or "").lower())[:60]


@app.get("/insights/data")
def insights_data():
    try:
        drivers = _bq_query(_CALL_DRIVERS_SQL)
        transcripts = _bq_query(_TRANSCRIPTS_SQL)
    except Exception as e:  # surface a clean message to the dashboard
        return {"error": str(e)[:300], "drivers": [], "transcripts": [], "total": 0}
    total = len(transcripts)  # distinct conversations (one row per conversation_id)
    # READ-ONLY: surface the REAL Contact Center AI Insights analysis (sentiment + entities) that
    # insights/cci_analyze.py pulled from CCAI into BigQuery. This GET only reads it — no analysis
    # is run here (no Cloud NL calls, no inserts → safe/idempotent).
    by_id, ent_by_id = {}, {}
    try:
        for r in _bq_query(f"SELECT conversation_id, sentiment, entities FROM `{BQ_INSIGHTS_TABLE}`"):
            by_id[r["conversation_id"]] = r.get("sentiment")
            ent_by_id[r["conversation_id"]] = r.get("entities") or ""
    except Exception:
        pass
    # Count sentiment over the CURRENT conversation set only, so "scored" can never exceed
    # "captured" (the table may hold rows from other/older apps). Tally top entities too.
    sentiment = {"positive": 0, "neutral": 0, "negative": 0}
    ent_counts = {}
    for t in transcripts:
        cid = _sani(t.get("conversation_id"))
        lab = by_id.get(cid)
        t["sentiment"] = lab
        if lab in sentiment:
            sentiment[lab] += 1
        for name in (e.strip() for e in ent_by_id.get(cid, "").split(",") if e.strip()):
            ent_counts[name.lower()] = ent_counts.get(name.lower(), 0) + 1
    top_entities = [{"name": k, "count": v} for k, v in
                    sorted(ent_counts.items(), key=lambda kv: kv[1], reverse=True)[:8]]
    return {"drivers": drivers, "transcripts": transcripts, "total": total,
            "sentiment": sentiment, "sentiment_total": sum(sentiment.values()),
            "entities": top_entities}


_INSIGHTS_UI = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Conversational Insights · Tilicho Credit</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='112' fill='%23E00917'/><g transform='translate(256 256) scale(1.15) translate(-150 -132)' fill='%23ffffff'><path d='M2 4 H128 V260 H78 V72 Q78 50 56 50 H2 Z'/><path d='M2 4 H128 V260 H78 V72 Q78 50 56 50 H2 Z' transform='rotate(180 150 132)'/></g></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--paper:#F2F3F1;--paper2:#FBFBFA;--pl:rgba(10,14,22,.10);--red:#E00917;--green:#39B98A;
 --hi:#11161E;--lo:#525C69;--mute:#8A929C;--disp:'Space Grotesk',sans-serif;--body:'IBM Plex Sans',sans-serif;--mono:'IBM Plex Mono',monospace}
*{box-sizing:border-box}body{margin:0;font-family:var(--body);color:var(--lo);background:var(--paper);min-height:100dvh;-webkit-font-smoothing:antialiased}
.hdwrap{background:#0F1319;border-bottom:1px solid rgba(255,255,255,.07)}
.hd{display:flex;align-items:center;gap:14px;max-width:1100px;margin:0 auto;padding:16px 28px}
.logo{display:flex;align-items:center;gap:9px;text-decoration:none}
.hdwrap .ttl h1{color:#fff}
.hdwrap .vsep{background:rgba(255,255,255,.16)}
.hdwrap .nav a{color:#B7BDC6}
.hdwrap .refresh{border-color:rgba(255,255,255,.18);color:#B7BDC6}
.hdwrap .refresh:hover,.hdwrap .nav a:hover{color:var(--red);border-color:var(--red)}
.vsep{width:1px;height:26px;background:var(--pl)}
.ttl .k{font-family:var(--mono);font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--mute)}
.ttl h1{font-family:var(--disp);font-size:15px;font-weight:600;margin:2px 0 0;color:var(--hi);letter-spacing:-.01em}
.nav{margin-left:auto;display:flex;gap:16px;align-items:center}
.nav a{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--lo);text-decoration:none;display:inline-flex;align-items:center;height:30px;line-height:1}
.nav a:hover{color:var(--red)}
.refresh{cursor:pointer;border:1px solid var(--pl);background:none;font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--lo);height:30px;padding:0 12px;display:inline-flex;align-items:center;line-height:1}
.refresh:hover{border-color:var(--red);color:var(--red)}
.wrap{max-width:1100px;margin:0 auto;padding:0 28px 60px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--pl);border:1px solid var(--pl);margin-top:34px}
@media(max-width:720px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi{background:var(--paper2);padding:20px}
.kpi .v{font-family:var(--disp);font-size:30px;font-weight:600;color:var(--hi);letter-spacing:-.02em}
.kpi .v.red{color:var(--red)}
.kpi .l{font-family:var(--mono);font-size:10px;letter-spacing:.13em;text-transform:uppercase;color:var(--mute);margin-top:7px}
.sec{display:flex;align-items:baseline;gap:11px;margin:40px 2px 14px}
.sec .tick{width:9px;height:9px;background:var(--red);flex:0 0 9px;position:relative;top:1px}
.sec h2{font-family:var(--disp);font-size:18px;font-weight:600;color:var(--hi);margin:0;letter-spacing:-.01em}
.sec em{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--mute);font-style:normal}
.card{background:var(--paper2);border:1px solid var(--pl);padding:20px 22px}
.bar-row{display:flex;align-items:center;gap:14px;margin:12px 0}
.bar-row .lab{width:130px;flex:0 0 130px;font-size:13.5px;color:var(--hi)}
.track{flex:1;background:#E6E7E4;height:22px;overflow:hidden}
.fill{height:100%;background:var(--hi);display:flex;align-items:center;justify-content:flex-end;padding-right:8px;color:var(--paper2);font-family:var(--mono);font-size:11px;min-width:26px;transition:width .9s cubic-bezier(.2,.8,.2,1)}
.bar-row .n{width:56px;flex:0 0 56px;text-align:right;font-family:var(--mono);font-size:12px;color:var(--mute)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);font-weight:500;padding:9px 10px;border-bottom:1px solid var(--pl)}
td{padding:11px 10px;border-bottom:1px solid var(--pl);vertical-align:top;line-height:1.5;color:var(--lo)}
td.who{white-space:nowrap;font-family:var(--mono);font-size:12px;color:var(--hi)}
.muted{color:var(--mute);font-size:13px}
.legend{display:flex;gap:20px;margin-top:13px;font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase}
.err{border:1px solid var(--red);color:#9a1018;padding:13px;margin-top:20px;font-size:13px}
.foot{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--mute);margin-top:36px;line-height:1.9;max-width:86ch}
</style></head><body>
<div class="hdwrap"><div class="hd">
 <a class="logo" href="https://tilicho.in" target="_blank" title="Tilicho Labs"><img src="/static/img/tilicho-logo.webp" alt="Tilicho Labs" style="height:26px;width:auto;display:block"></a>
 <div class="vsep"></div>
 <div class="ttl"><div class="k">Conversational Insights · Pillar 4</div><h1>Customer experience analytics</h1></div>
 <div class="nav"><a href="/agent">Agent ↗</a><button class="refresh" id="rf">↻ Refresh</button></div>
</div></div>
<div class="wrap">
 <div class="kpis" id="kpis"></div>
 <div class="sec"><span class="tick"></span><h2>Call drivers</h2><em>why customers contact us</em></div>
 <div class="card" id="chart"><div class="muted">Loading…</div></div>
 <div class="sec"><span class="tick"></span><h2>Customer sentiment</h2><em>Contact Center AI Insights · Google ML</em></div>
 <div class="card" id="sent"><div class="muted">Loading…</div></div>
 <div class="sec"><span class="tick"></span><h2>Top entities</h2><em>Contact Center AI Insights · extracted</em></div>
 <div class="card" id="ent"><div class="muted">Loading…</div></div>
 <div class="sec"><span class="tick"></span><h2>Transcript review</h2><em>borrower said · agent replied</em></div>
 <div class="card" id="table"><div class="muted">Loading…</div></div>
 <div id="err"></div>
 <p class="foot">Conversations are captured 100% and auto-ingested into the native
  <b>Contact Center AI Insights</b> product, which computes the <b>sentiment &amp; entities</b> shown
  here (real Google ML, model V2). This page surfaces CCAI's analysis (pulled via API by
  insights/cci_analyze.py); the native CX Insights console (location us) is the source of truth.
  Call drivers are keyword rules; topic modelling &amp; Quality-AI are the native product's next layer
  (need conversation volume). Demo data — synthetic, self-generated.</p>
</div>
<script>
function esc(t){return (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
async function load(){
 document.getElementById('err').innerHTML='';
 let d;try{d=await (await fetch('/insights/data')).json();}catch(e){d={error:'Could not load data'};}
 if(d.error){document.getElementById('err').innerHTML='<div class="err">'+esc(d.error)+'</div>';}
 const drivers=d.drivers||[],total=d.total||0;
 const sm=d.sentiment||{positive:0,neutral:0,negative:0},smT=d.sentiment_total||0;
 // KPIs (red accent only on the actionable 'flagged negative')
 const kpis=[[total,'Conversations captured',''],[drivers.length,'Distinct call drivers',''],
   [smT+' / '+total,'Scored for sentiment',''],[sm.negative||0,'Flagged negative','red']];
 document.getElementById('kpis').innerHTML=kpis.map(k=>
   '<div class="kpi"><div class="v '+k[2]+'">'+k[0]+'</div><div class="l">'+k[1]+'</div></div>').join('');
 // sentiment bar (real Google ML) — monochrome, red = negative (the signal)
 const sc={positive:'#11161E',neutral:'#8A929C',negative:'#E00917'},
   sf={positive:'#11161E',neutral:'#C9CCC9',negative:'#E00917'},
   stc={positive:'#FBFBFA',neutral:'#11161E',negative:'#fff'};
 if(smT){let bar='<div style="display:flex;height:24px;overflow:hidden;border:1px solid var(--pl)">';
  ['positive','neutral','negative'].forEach(k=>{const w=Math.round(100*sm[k]/smT);
   if(w>0)bar+='<div style="width:'+w+'%;background:'+sf[k]+';display:flex;align-items:center;justify-content:center;color:'+stc[k]+';font-family:var(--mono);font-size:11px">'+sm[k]+'</div>';});
  bar+='</div><div class="legend">'+
   ['positive','neutral','negative'].map(k=>'<span style="color:'+sc[k]+'">'+k+' · '+sm[k]+'</span>').join('')+'</div>';
  document.getElementById('sent').innerHTML=bar;
 } else {document.getElementById('sent').innerHTML='<div class="muted">No sentiment yet — run insights/cci_analyze.py.</div>';}
 // top entities (Contact Center AI Insights)
 const ents=d.entities||[];
 if(ents.length){
  const emax=Math.max(...ents.map(e=>+e.count));
  document.getElementById('ent').innerHTML=ents.map(e=>{
   const w=Math.max(8,Math.round(100*(+e.count)/emax));
   return '<div class="bar-row"><div class="lab">'+esc(e.name)+'</div>'+
    '<div class="track"><div class="fill" style="width:'+w+'%">'+e.count+'</div></div></div>';}).join('');
 } else {document.getElementById('ent').innerHTML='<div class="muted">No entities yet — run insights/cci_analyze.py.</div>';}
 // chart
 if(drivers.length){
  const max=Math.max(...drivers.map(x=>+x.conversations));
  document.getElementById('chart').innerHTML=drivers.map(x=>{
   const w=Math.max(8,Math.round(100*(+x.conversations)/max));
   return '<div class="bar-row"><div class="lab">'+esc(x.call_driver)+'</div>'+
    '<div class="track"><div class="fill" style="width:'+w+'%">'+x.conversations+'</div></div>'+
    '<div class="n">'+(x.pct||'')+'%</div></div>';}).join('');
 } else {document.getElementById('chart').innerHTML='<div class="muted">No conversations yet.</div>';}
 // table
 const ts=d.transcripts||[];
 if(ts.length){
  const scol={positive:'#11161E',neutral:'#8A929C',negative:'#E00917'};
  document.getElementById('table').innerHTML='<table><tr><th>Conversation</th><th>Sentiment</th><th>Borrower said</th><th>Agent replied</th></tr>'+
   ts.map(r=>{const sv=r.sentiment||'—';return '<tr><td class="who">'+esc(r.conversation_id)+'</td><td style="color:'+(scol[sv]||"#9aa1ad")+';font-weight:500">'+esc(sv)+'</td><td>'+esc(r.borrower_said)+'</td><td>'+esc(r.agent_replied)+'</td></tr>';}).join('')+'</table>';
 } else {document.getElementById('table').innerHTML='<div class="muted">No transcripts yet.</div>';}
}
document.getElementById('rf').onclick=load;load();
</script></body></html>"""


@app.get("/insights", response_class=HTMLResponse)
def insights_ui():
    return _INSIGHTS_UI


# =============================================================================
# Rep handoff console (WIP, unadvertised): given an escalated transcript, returns a summary,
# suggested replies and grounded knowledge from the CES `assist` agent. The native-product
# wiring is parked (see agent/agent_assist.py); this route is not linked from the demo.
# =============================================================================
ASSIST_AGENT = f"{CES_APP}/agents/assist"


@app.get("/assist/data")
def assist_data(cid: str = ""):
    try:
        rows = _bq_query(_TRANSCRIPTS_SQL)
    except Exception as e:
        return {"error": str(e)[:200]}
    if not rows:  # graceful empty state (no specific seed conversation required)
        return {"conversation_id": None, "transcript": "",
                "summary": "No conversations captured yet — chat with the agent, then refresh.",
                "replies": [], "knowledge": "", "conversations": []}
    conv = next((r for r in rows if r.get("conversation_id") == cid), rows[0])
    transcript = f"Borrower: {conv.get('borrower_said') or ''}\nAgent: {conv.get('agent_replied') or ''}"
    res = _run_session(f"assist-{cid}", f"Escalated conversation:\n{transcript}\n\nProvide rep assist.",
                       ASSIST_AGENT)
    text = res.get("reply") or ""

    def sec(tag, nxt):
        pat = r"\[" + tag + r"\](.*?)(?=\[" + nxt + r"\]|$)" if nxt else r"\[" + tag + r"\](.*)$"
        m = re.search(pat, text, re.S)
        return m.group(1).strip() if m else ""

    replies = [re.sub(r"^[-*]\s*", "", l).strip()
               for l in sec("REPLIES", "KNOWLEDGE").splitlines() if l.strip()]
    return {"conversation_id": cid, "transcript": transcript,
            "summary": sec("SUMMARY", "REPLIES"), "replies": replies,
            "knowledge": sec("KNOWLEDGE", None), "raw": text,
            "rate_limited": res.get("rate_limited"),
            "conversations": [r.get("conversation_id") for r in rows]}


_ASSIST_UI = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rep console · Tilicho Credit</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='112' fill='%23E00917'/><g transform='translate(256 256) scale(1.15) translate(-150 -132)' fill='%23ffffff'><path d='M2 4 H128 V260 H78 V72 Q78 50 56 50 H2 Z'/><path d='M2 4 H128 V260 H78 V72 Q78 50 56 50 H2 Z' transform='rotate(180 150 132)'/></g></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--ink:#0A0E16;--ink2:#0F141E;--ink3:#161D29;--il:rgba(255,255,255,.09);--red:#E00917;--green:#39B98A;
 --hi:#EBEEF3;--lo:#8A96A6;--mute:#5E6A7A;--disp:'Space Grotesk',sans-serif;--body:'IBM Plex Sans',sans-serif;--mono:'IBM Plex Mono',monospace}
*{box-sizing:border-box}body{margin:0;font-family:var(--body);color:var(--lo);background:var(--ink);min-height:100dvh;-webkit-font-smoothing:antialiased}
.hd{display:flex;align-items:center;gap:14px;max-width:1180px;margin:0 auto;padding:18px 28px;border-bottom:1px solid var(--il)}
.logo{display:flex;align-items:center;gap:9px;text-decoration:none}
.logo .wm{font-family:var(--disp);font-weight:600;font-size:16px;letter-spacing:-.01em;color:var(--hi)}
.logo .wm span{color:var(--mute)}
.vsep{width:1px;height:26px;background:var(--il)}
.ttl .k{font-family:var(--mono);font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--mute)}
.ttl h1{font-family:var(--disp);font-size:15px;font-weight:600;margin:2px 0 0;color:var(--hi)}
.nav{margin-left:auto;display:flex;gap:14px;align-items:center}
.nav a{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--lo);text-decoration:none;display:inline-flex;align-items:center;height:30px;line-height:1}
.nav a:hover{color:var(--red)}
.sel,.regen{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--lo);background:none;border:1px solid var(--il);height:30px;padding:0 10px;cursor:pointer;display:inline-flex;align-items:center;line-height:1}
.sel option{color:#111}
.regen:hover,.sel:hover{border-color:var(--red);color:var(--red)}
.wrap{max-width:1180px;margin:0 auto;padding:26px 28px 60px;display:grid;grid-template-columns:1fr 1.1fr;gap:24px}
@media(max-width:860px){.wrap{grid-template-columns:1fr}}
.sec{display:flex;align-items:baseline;gap:10px;margin:0 2px 12px}
.sec .tick{width:8px;height:8px;background:var(--red);flex:0 0 8px;position:relative;top:1px}
.sec h2{font-family:var(--disp);font-size:14px;font-weight:600;color:var(--hi);margin:0}
.sec .t{font-family:var(--mono);font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);margin-left:auto;display:flex;align-items:center;gap:6px}
.sec .dot{width:6px;height:6px;border-radius:50%;background:var(--green)}
.card{background:var(--ink2);border:1px solid var(--il);padding:18px;margin-bottom:16px}
.tx{white-space:pre-wrap;line-height:1.65;font-size:14px;color:var(--hi)}
.tx b{color:var(--red);font-weight:500}
.lbl{font-family:var(--mono);font-size:9.5px;font-weight:500;text-transform:uppercase;letter-spacing:.16em;color:var(--mute);margin-bottom:10px}
.sumtxt{font-size:15px;line-height:1.6;color:var(--hi)}
.reply{display:block;width:100%;text-align:left;background:none;border:1px solid var(--il);padding:11px 13px;margin:8px 0;font:inherit;font-size:13.5px;color:var(--lo);cursor:pointer;transition:.12s}
.reply:hover{color:var(--hi);border-color:var(--mute);box-shadow:inset 2px 0 0 var(--red)}
.know{border:1px solid var(--il);border-left:2px solid var(--green);padding:12px 14px;font-size:13.5px;line-height:1.6;color:var(--hi);background:var(--ink)}
.muted{color:var(--mute);font-size:13px}
.compose{margin-top:8px}
.compose textarea{width:100%;border:1px solid var(--il);background:var(--ink);color:var(--hi);padding:10px;font:inherit;font-size:14px;min-height:56px;resize:vertical;outline:none}
.compose textarea:focus{border-color:var(--red)}
</style></head><body>
<div class="hd">
 <a class="logo" href="https://tilicho.in" target="_blank" title="Tilicho Labs">
  <img src="/static/img/tilicho-logo.webp" alt="Tilicho Labs" style="height:26px;width:auto;display:block"></a>
 <div class="vsep"></div>
 <div class="ttl"><div class="k">Tilicho Credit · internal</div><h1>Rep console</h1></div>
 <div class="nav"><select class="sel" id="pick"></select><button class="regen" id="rg">↻ Regenerate</button><a href="/agent">Agent ↗</a><a href="/insights">Insights ↗</a></div>
</div>
<div class="wrap">
 <div>
  <div class="sec"><span class="tick"></span><h2>Escalated conversation</h2><span class="t" id="cid"></span></div>
  <div class="card tx" id="transcript"><span class="muted">Loading…</span></div>
 </div>
 <div class="assist">
  <div class="sec"><span class="tick"></span><h2>AI assist for the rep</h2><span class="t"><span class="dot"></span>Gemini · live</span></div>
  <div class="card"><div class="lbl">Summary</div><div class="sumtxt" id="summary"><span class="muted">…</span></div></div>
  <div class="card"><div class="lbl">Suggested replies</div><div id="replies"><span class="muted">…</span></div>
   <div class="compose"><textarea id="draft" placeholder="Reply draft — click a suggestion to load…"></textarea></div></div>
  <div class="card"><div class="lbl">Knowledge assist</div><div class="know" id="knowledge"><span class="muted">…</span></div></div>
 </div>
</div>
<script>
function esc(t){return (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function fmt(t){return esc(t).replace(/\\*\\*(.+?)\\*\\*/g,'<b>$1</b>').replace(/\\n/g,'<br>');}
const pick=document.getElementById('pick');
async function load(cid){
 document.getElementById('summary').innerHTML='<span class="muted">Generating…</span>';
 document.getElementById('replies').innerHTML='<span class="muted">Generating…</span>';
 document.getElementById('knowledge').innerHTML='<span class="muted">Generating…</span>';
 let d;try{d=await (await fetch('/assist/data'+(cid?'?cid='+encodeURIComponent(cid):''))).json();}catch(e){d={error:'load failed'};}
 if(d.conversations&&!pick.options.length){pick.innerHTML=d.conversations.map(c=>'<option'+(c===d.conversation_id?' selected':'')+'>'+esc(c)+'</option>').join('');}
 document.getElementById('cid').textContent=d.conversation_id||'';
 document.getElementById('transcript').innerHTML=fmt((d.transcript||'').replace(/^Borrower:/m,'**Borrower:**').replace(/\\nAgent:/,'\\n**Agent:**'))||'<span class="muted">No transcript.</span>';
 if(d.rate_limited){document.getElementById('summary').innerHTML='<span class="muted">Rate limited — try Regenerate in ~30s.</span>';document.getElementById('replies').innerHTML='';document.getElementById('knowledge').innerHTML='';return;}
 document.getElementById('summary').innerHTML=fmt(d.summary)||'<span class="muted">—</span>';
 document.getElementById('replies').innerHTML=(d.replies&&d.replies.length)?d.replies.map(r=>'<button class="reply">'+esc(r)+'</button>').join(''):'<span class="muted">—</span>';
 document.querySelectorAll('.reply').forEach(b=>b.onclick=()=>{document.getElementById('draft').value=b.textContent;});
 document.getElementById('knowledge').innerHTML=fmt(d.knowledge)||'<span class="muted">—</span>';
}
pick.onchange=()=>load(pick.value);
document.getElementById('rg').onclick=()=>load(pick.value);
load();
</script></body></html>"""


@app.get("/assist", response_class=HTMLResponse)
def assist_ui():
    return _ASSIST_UI


# =============================================================================
# Voice (Pillar 2): HD text-to-speech (Chirp 3) so the agent can be heard. Paired
# with the browser's speech recognition in the UI for a full speak/listen loop.
# =============================================================================
TTS_VOICE = os.environ.get("TTS_VOICE", "en-IN-Chirp3-HD-Aoede")


@app.get("/tts")
def tts(text: str):
    """Synthesize text to speech with a Chirp 3 HD voice; returns MP3 audio."""
    url = "https://texttospeech.googleapis.com/v1/text:synthesize"
    body = json.dumps({
        "input": {"text": text[:1500]},
        "voice": {"languageCode": "en-IN", "name": TTS_VOICE},
        "audioConfig": {"audioEncoding": "MP3"},
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {_ces_token()}")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Goog-User-Project", PROJECT_ID)
    try:
        with urllib.request.urlopen(req) as r:
            audio = json.loads(r.read().decode()).get("audioContent", "")
        return Response(content=base64.b64decode(audio), media_type="audio/mpeg")
    except urllib.error.HTTPError as e:
        return Response(content=e.read()[:300], status_code=e.code)


# =============================================================================
# Walkthrough (/tour): a single-page, Bellatrix-style interactive presentation that
# PROVES this is a real, code-first POC on Gemini Enterprise for CX, built with CXAS
# SCRAPI. Self-contained + uses absolute Cloud Run URLs, so the same page can later be
# served from a Cloudflare Worker unchanged. Embeds the live agent + live UI previews.
# =============================================================================
_TOUR_UI = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tilicho Credit Assist — POC Walkthrough</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='112' fill='%23E00917'/><g transform='translate(256 256) scale(1.15) translate(-150 -132)' fill='%23ffffff'><path d='M2 4 H128 V260 H78 V72 Q78 50 56 50 H2 Z'/><path d='M2 4 H128 V260 H78 V72 Q78 50 56 50 H2 Z' transform='rotate(180 150 132)'/></g></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--ink:#0A0E16;--ink2:#0F141E;--ink3:#161D29;--il:rgba(255,255,255,.09);--red:#E00917;
 --green:#39B98A;--hi:#EBEEF3;--lo:#8A96A6;--mute:#5E6A7A;
 --disp:'Space Grotesk',sans-serif;--body:'IBM Plex Sans',sans-serif;--mono:'IBM Plex Mono',monospace}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:70px}
body{margin:0;font-family:var(--body);color:var(--lo);background:var(--ink);-webkit-font-smoothing:antialiased;line-height:1.6}
a{color:inherit}
.mono{font-family:var(--mono);text-transform:uppercase;letter-spacing:.16em}
/* header */
.hd{position:sticky;top:0;z-index:50;padding:13px 0;
 background:rgba(10,14,22,.82);backdrop-filter:blur(10px);border-bottom:1px solid var(--il)}
.hdin{max-width:1080px;margin:0 auto;padding:0 24px;display:flex;align-items:center;gap:14px}
.logo{display:flex;align-items:center;gap:9px;text-decoration:none}
.logo .wm{font-family:var(--disp);font-weight:600;font-size:15px;letter-spacing:-.01em;color:var(--hi)}
.logo .wm span{color:var(--mute)}
.vsep{width:1px;height:24px;background:var(--il)}
.htitle{font-family:var(--mono);font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--mute)}
.hnav{margin-left:auto;display:flex;gap:18px;align-items:center}
.hnav a.lnk{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--lo);text-decoration:none}
.hnav a.lnk:hover{color:var(--red)}
@media(max-width:820px){.hnav .lnk{display:none}}
.btn{font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;text-decoration:none;
 padding:9px 14px;border:1px solid var(--il);color:var(--hi);display:inline-flex;align-items:center;gap:7px;cursor:pointer;background:none}
.btn:hover{border-color:var(--red);color:var(--red)}
.btn.solid{background:var(--red);color:#fff;border-color:var(--red)}.btn.solid:hover{background:#c00813;color:#fff}
/* layout */
.inner{max-width:1080px;margin:0 auto;padding:0 24px}
section{padding:64px 0}
section.light{background:#F4F4F2;color:#3F4651;
 --ink:#fff;--ink2:#fff;--ink3:#ECECE8;--il:rgba(10,14,22,.13);
 --hi:#11161E;--lo:#3F4651;--mute:#7A828C}
.kick{font-family:var(--mono);font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--red)}
h1{font-family:var(--disp);font-weight:600;color:var(--hi);font-size:clamp(34px,6vw,58px);line-height:1.02;letter-spacing:-.02em;margin:18px 0 0}
h2{font-family:var(--disp);font-weight:600;color:var(--hi);font-size:26px;letter-spacing:-.01em;margin:0}
h3{font-family:var(--disp);font-weight:600;color:var(--hi);font-size:16px;margin:0 0 4px}
.lead{font-size:18px;color:var(--lo);max-width:60ch;margin:20px 0 0}
.sechead{display:flex;align-items:baseline;gap:12px;margin-bottom:26px}
.sechead .tick{width:9px;height:9px;background:var(--red);flex:0 0 9px;position:relative;top:2px}
.sechead em{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);font-style:normal;margin-left:auto}
/* hero */
.hero{padding-top:64px}
.badges{display:flex;flex-wrap:wrap;gap:9px;margin-top:26px}
.badge{font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;padding:6px 11px;border:1px solid var(--il);color:var(--lo)}
.badge.g{color:var(--green);border-color:rgba(57,185,138,.35)}
.badge.r{color:var(--red);border-color:rgba(224,9,23,.4)}
.cta{display:flex;flex-wrap:wrap;gap:11px;margin-top:30px}
/* generic cards */
.grid{display:grid;gap:1px;background:var(--il);border:1px solid var(--il)}
.cols-2{grid-template-columns:1fr 1fr}.cols-3{grid-template-columns:repeat(3,1fr)}.cols-4{grid-template-columns:repeat(4,1fr)}
@media(max-width:760px){.cols-2,.cols-3,.cols-4{grid-template-columns:1fr}}
.cell{background:var(--ink2);padding:20px}
.cell .l{font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--mute);margin-bottom:8px}
.cell .v{color:var(--hi);font-size:14px}
.cell .v.mono{font-family:var(--mono);text-transform:none;letter-spacing:0;font-size:12px;word-break:break-all;color:var(--hi)}
.feat .ic{color:var(--red);font-family:var(--mono);font-size:13px;margin-bottom:9px}
/* what-it-does / how-it-works table */
.cap{width:100%;border-collapse:collapse;margin-top:6px}
.cap td{border-top:1px solid var(--il);padding:14px 6px;vertical-align:top}
.cap tr:first-child td{border-top:0}
.cap .w{font-family:var(--disp);font-weight:600;color:var(--hi);font-size:15px;width:32%;padding-right:20px}
.cap .h{color:var(--lo);font-size:14px;line-height:1.55}
.cap code{font-family:var(--mono);font-size:12px;color:var(--hi)}
@media(max-width:640px){.cap .w,.cap .h{display:block;width:auto;padding:0}.cap .w{padding-top:14px}.cap .h{padding-bottom:14px;padding-top:4px}}
/* two-col */
.split{display:grid;grid-template-columns:1.3fr 1fr;gap:24px;align-items:start}
@media(max-width:760px){.split{grid-template-columns:1fr}}
.persona{background:var(--ink2);border:1px solid var(--il);padding:22px}
.persona .l{font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--mute)}
.persona .nm{font-family:var(--disp);font-size:20px;color:var(--hi);margin:6px 0 14px}
.prow{display:flex;justify-content:space-between;padding:9px 0;border-top:1px solid var(--il);font-size:14px}
.prow span:first-child{color:var(--lo)}.prow span:last-child{color:var(--hi);font-family:var(--mono);font-size:13px}
/* chat widget */
.chat{border:1px solid var(--il);background:var(--ink2);display:flex;flex-direction:column;height:460px;max-width:760px}
.clog{flex:1;overflow-y:auto;padding:18px;display:flex;flex-direction:column;gap:14px}
.crow{display:flex;gap:10px;align-items:flex-start;max-width:88%}
.crow.user{align-self:flex-end;flex-direction:row-reverse}
.cav{width:26px;height:26px;flex:0 0 26px;display:grid;place-items:center;border:1px solid var(--il);background:var(--ink3)}
.cav.user{font-family:var(--mono);font-size:8.5px;color:var(--mute)}
.cbub{padding:11px 14px;border:1px solid var(--il);background:var(--ink);color:var(--hi);font-size:14px;white-space:pre-wrap}
.user .cbub{background:var(--ink3)}
.cbub.err{border-color:var(--red);color:#f3b6ba}
.cmeta{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.cchip{font-family:var(--mono);font-size:9px;letter-spacing:.1em;text-transform:uppercase;padding:2px 7px;border:1px solid var(--il);color:var(--mute)}
.cchip.g{color:var(--green);border-color:rgba(57,185,138,.35)}
.cbar{display:flex;gap:7px;flex-wrap:wrap;padding:10px 14px 0}
.cbar button{cursor:pointer;background:none;border:1px solid var(--il);padding:7px 10px;font-family:var(--mono);font-size:10px;letter-spacing:.05em;color:var(--lo)}
.cbar button:hover{color:var(--hi);border-color:var(--mute);box-shadow:inset 2px 0 0 var(--red)}
.cform{display:flex;gap:10px;padding:12px 14px;border-top:1px solid var(--il);align-items:center}
.cform input{flex:1;padding:11px 4px;border:0;border-bottom:1px solid var(--il);background:none;color:var(--hi);font-size:14px;outline:none;font-family:inherit}
.cform input:focus{border-bottom-color:var(--red)}
.cform button{width:40px;height:40px;border:0;background:var(--red);color:#fff;cursor:pointer;font-size:16px}
.cform button:disabled{opacity:.4}
/* pillars */
.pillar{background:var(--ink2);border:1px solid var(--il);padding:0;margin-bottom:16px;overflow:hidden}
.phead{display:flex;align-items:center;gap:16px;padding:18px 20px}
.pnum{font-family:var(--disp);font-size:22px;color:var(--red);font-weight:600;flex:0 0 auto}
.phead .pt{flex:1}.phead .pt p{margin:3px 0 0;font-size:13.5px;color:var(--lo)}
.pacts{display:flex;gap:9px;flex:0 0 auto}
@media(max-width:680px){.phead{flex-wrap:wrap}.pacts{width:100%}}
.frame{display:none;border-top:1px solid var(--il);height:560px;background:#000}
.frame iframe{width:100%;height:100%;border:0;display:block}
.frame.dark{background:var(--ink)}
/* code */
.code{background:#070a10;border:1px solid var(--il);padding:16px 18px;overflow-x:auto;font-family:var(--mono);
 font-size:12px;line-height:1.7;color:#cdd6e3;white-space:pre;margin:0}
.code .c{color:var(--mute)}.code .k{color:var(--green)}.code .s{color:#e8b07a}
.tree{font-family:var(--mono);font-size:12px;line-height:1.85;color:var(--lo);white-space:pre}
.tree b{color:var(--hi);font-weight:500}
/* console slot */
.slot{border:1px dashed var(--il);background:var(--ink2);padding:40px 20px;text-align:center;color:var(--mute)}
.slot .big{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--lo)}
.slot small{display:block;margin-top:8px;font-size:12px;color:var(--mute);max-width:60ch;margin-left:auto;margin-right:auto}
.shots{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
@media(max-width:720px){.shots{grid-template-columns:1fr}}
.shots figure{margin:0}
.shots img{width:100%;display:block;border:1px solid var(--il);border-radius:8px;background:var(--ink2)}
.shots figcaption{margin-top:8px;font-size:12px;color:var(--mute);font-family:var(--mono);letter-spacing:.04em}
.shots .ph{display:none;border:1px dashed var(--il);background:var(--ink2);padding:42px 14px;text-align:center;color:var(--mute);font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;border-radius:8px}
.shots figure.miss img{display:none}
.shots figure.miss .ph{display:block}
.note{font-family:var(--mono);font-size:10px;letter-spacing:.06em;color:var(--mute);margin-top:10px}
.foot{padding:34px 0 60px;color:var(--mute);font-family:var(--mono);font-size:10.5px;letter-spacing:.1em;text-transform:uppercase}
.foot a{color:var(--lo);text-decoration:none}.foot a:hover{color:var(--red)}
.subhead{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--red);margin:34px 0 14px}
.archd{border:1px solid #E5E5E2;background:#FFFFFF;border-radius:14px;padding:24px 20px;overflow-x:auto}
.cp{display:flex;flex-direction:column;font-family:'IBM Plex Mono',monospace;max-width:880px;margin:0 auto}
.cp-build{align-self:center;font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:#1f8a66;border:1px dashed #bfe0d3;border-radius:6px;padding:5px 11px;margin-bottom:14px}
.cp-build b{color:#16734f;font-weight:600}
.cp-lane{border:1px solid #E6E6E3;border-radius:10px;background:#FBFBFA;padding:16px;text-align:center}
.cp-runtime{border-color:#E00917;background:#fff}
.cp-tag{display:block;font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:#9aa0a8;margin-bottom:12px}
.cp-tag.red{color:#E00917}.cp-tag.green{color:#1f8a66}
.cp-node{display:inline-flex;flex-direction:column;gap:3px;align-items:center;border:1px solid #E6E6E3;border-radius:8px;background:#fff;padding:12px 18px;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:14px;color:#11161E;max-width:600px}
.cp-node.green{border-color:#bfe0d3}
.cp-sub{font-family:'IBM Plex Mono',monospace;font-weight:400;font-size:10.5px;letter-spacing:.02em;color:#6b7280;text-transform:none}
.cp-bk{display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:760px;margin:0 auto;text-align:left}
@media(max-width:640px){.cp-bk{grid-template-columns:1fr}}
.cp-bk .cp-node{display:flex;align-items:flex-start;max-width:none}
.cp-stages{display:flex;align-items:stretch;gap:8px;flex-wrap:wrap}
.cp-stg{flex:1;min-width:104px;text-align:left;border:1px solid #E6E6E3;border-radius:8px;background:#fff;padding:12px;font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:13.5px;color:#11161E;cursor:pointer;transition:.15s}
.cp-stg:hover{border-color:#b9bdc4}
.cp-stg.active{border-color:#E00917;background:#FDEEEF}
.cp-stg .cp-n{display:block;font-family:'IBM Plex Mono',monospace;font-size:10px;color:#E00917;margin-bottom:6px}
.cp-arr{align-self:center;font-style:normal;color:#c2c5cb;font-size:13px;animation:cpflow 1.6s linear infinite}
.cp-arr:nth-of-type(2){animation-delay:.15s}.cp-arr:nth-of-type(3){animation-delay:.3s}.cp-arr:nth-of-type(4){animation-delay:.45s}
@keyframes cpflow{0%,100%{opacity:.35}50%{opacity:.9}}
.cp-detail{margin-top:14px;border-top:1px solid #ECECEA;padding-top:12px;font-family:'IBM Plex Mono',monospace;font-size:12.5px;line-height:1.65;color:#4b5563;min-height:46px;text-align:left}
.cp-detail b{color:#11161E;font-weight:600}
.cp-conn{height:34px;position:relative;display:flex;align-items:center;justify-content:center}
.cp-conn:before{content:"";position:absolute;top:0;bottom:0;left:50%;width:2px;transform:translateX(-50%);background:repeating-linear-gradient(#cfd2d6 0 4px,transparent 4px 9px)}
.cp-pkt{position:absolute;left:50%;width:7px;height:7px;margin-left:-3.5px;border-radius:50%;background:#E00917;box-shadow:0 0 7px rgba(224,9,23,.55);animation:cppkt 1.6s ease-in-out infinite}
@keyframes cppkt{0%{top:-3px;opacity:0}12%{opacity:1}88%{opacity:1}100%{top:34px;opacity:0}}
.cp-down{font-size:8.5px;letter-spacing:.12em;text-transform:uppercase;color:#9aa0a8;background:#fff;padding:2px 9px;position:relative;z-index:1;border-radius:4px}
.cp-tap{margin-top:12px;border:1px solid #bfe0d3;border-radius:10px;background:rgba(31,138,102,.05);padding:14px 16px;text-align:center}
.cp-tapflow{display:block;margin-top:3px;font-size:11.5px;color:#6b7280;letter-spacing:.02em}
</style></head><body>

<div class="hd"><div class="hdin">
 <a class="logo" href="https://tilicho.in" target="_blank"><img src="/static/img/tilicho-logo.webp" alt="Tilicho Labs" style="height:26px;width:auto;display:block"></a>
 <div class="vsep"></div><span class="htitle">POC Walkthrough</span>
 <div class="hnav">
  <a class="lnk" href="#what">What</a>
  <a class="lnk" href="#tour">Tour</a><a class="lnk" href="#architecture">Stack</a><a class="lnk" href="#implementation">Implementation</a>
  <a class="btn" href="/agent" target="_blank">Open app ↗</a>
 </div>
</div></div>

<section class="hero" id="top">
  <div class="inner">
  <div class="kick">POC · Google Gemini Enterprise for Customer Experience</div>
  <h1>Tilicho Credit Assist</h1>
  <p class="lead">A customer-support AI agent for a digital lender — it answers borrowers'
   loan questions, acts on their account, stays compliant, and helps human agents. Built from
   code on Google's Gemini Enterprise for Customer Experience, with the official
   <b style="color:var(--hi)">CXAS SCRAPI</b> toolkit.</p>
  <div class="badges">
   <span class="badge">Gemini Enterprise for CX</span>
   <span class="badge r">Built with CXAS SCRAPI</span>
   <span class="badge g">6/6 golden evals</span>
   <span class="badge">Live on Google Cloud</span>
  </div>
  <div class="cta">
   <a class="btn solid" href="/agent" target="_blank">▶ Open the live agent</a>
   <a class="btn" href="#implementation">See the implementation ↓</a>
  </div>
  </div>
 </section>

 <section class="light" id="what">
  <div class="inner">
  <div class="sechead"><span class="tick"></span><h2>What this is</h2><em>use case · demo persona</em></div>
  <div class="split">
   <div>
    <p style="margin-top:0">A borrower chats (or talks) to <b style="color:var(--hi)">Tilicho Credit
     Assist</b> about their loan. The demo signs in as one borrower — <b style="color:var(--hi)">Asha R.</b>,
     loan <b style="color:var(--hi)">TL-1001</b>, verified by loan ID + last-4 phone. Only the loan
     back-office is a stand-in; everything else is live Google Cloud, and all data is synthetic.</p>
   </div>
   <div class="persona">
    <div class="l">Demo borrower</div><div class="nm">Asha R.</div>
    <div class="prow"><span>Loan ID</span><span>TL-1001</span></div>
    <div class="prow"><span>Verify (phone last-4)</span><span>4417</span></div>
    <div class="prow"><span>Product</span><span>Personal Loan</span></div>
    <div class="prow"><span>EMI</span><span>₹8,980</span></div>
    <div class="prow"><span>Outstanding</span><span>₹1,81,240</span></div>
    <div class="prow"><span>Late fee (policy)</span><span>₹500</span></div>
   </div>
  </div>
  <div class="subhead">What it does · how it works</div>
  <table class="cap">
   <tr><td class="w">Looks up loan accounts</td><td class="h">Tool calls to the lender's LMS — <code>getAccountSummary</code>, <code>getPayoffQuote</code> — after verifying the borrower (loan ID + last-4 phone).</td></tr>
   <tr><td class="w">Raises requests</td><td class="h">Files tickets for restructuring, complaints or KYC via the LMS <code>createTicket</code> tool, and returns a ticket ID + SLA.</td></tr>
   <tr><td class="w">Answers policy questions</td><td class="h">Pulls from the policy documents &amp; terms-and-conditions stored in the Vertex AI RAG corpus, and quotes the exact figures — not world knowledge.</td></tr>
   <tr><td class="w">Stays compliant</td><td class="h">A guardrail blocks legal / tax / investment advice and guaranteed outcomes, replacing them with a safe reply.</td></tr>
   <tr><td class="w">Speaks &amp; listens (in chat)</td><td class="h">Inside the web chat: 🔊 voice out via <b style="color:var(--hi)">Google Chirp-3</b> HD TTS and 🎤 speech-to-text in via the <b style="color:var(--hi)">browser</b> (Chrome/Edge) — a feature of the chat, not a separate telephony channel. CES session memory carries context across turns.</td></tr>
   <tr><td class="w">Turns chats into insights</td><td class="h">Every conversation auto-ingests into the native <b style="color:var(--hi)">Contact Center AI Insights</b> product (real Google ML sentiment + entities); the dashboard surfaces that analysis alongside keyword call-driver breakdowns.</td></tr>
  </table>
  </div>
 </section>

 <section id="tour">
  <div class="inner">
  <div class="sechead"><span class="tick"></span><h2>Guided tour</h2><em>open each live screen in a new tab</em></div>

  <div class="pillar">
   <div class="phead"><div class="pnum">01</div>
    <div class="pt"><h3>Conversational agent — tools, grounding, guardrail &amp; voice</h3><p>The borrower-facing chat: real tool calls, grounded policy answers, compliance, ₹ formatting, transparency tags — plus 🎤 speak / 🔊 listen in-browser (Google Chirp-3 HD voice) as a feature of the chat.</p></div>
    <div class="pacts"><a class="btn" href="/agent" target="_blank">Open ↗</a></div></div>
  </div>

  <div class="pillar">
   <div class="phead"><div class="pnum">02</div>
    <div class="pt"><h3>Conversational Insights — analytics</h3><p>Every conversation auto-ingests into <b style="color:var(--hi)">Contact Center AI Insights</b> (real Google ML sentiment + entities); the dashboard surfaces it + keyword call-drivers.</p></div>
    <div class="pacts"><a class="btn" href="/insights" target="_blank">Open ↗</a></div></div>
  </div>
  </div>
 </section>

 <section class="light" id="implementation">
  <div class="inner">
  <div class="sechead"><span class="tick"></span><h2>Implementation details</h2><em>built with CXAS SCRAPI · open by design</em></div>

  <div class="subhead">Live Google Cloud resources</div>
  <div class="grid cols-2">
   <div class="cell"><div class="l">Project</div><div class="v mono">gcex-pilot-16862 · no. 804472053350</div></div>
   <div class="cell"><div class="l">CES region</div><div class="v mono">us</div></div>
   <div class="cell"><div class="l">CXAS app (built with SCRAPI)</div><div class="v mono">projects/gcex-pilot-16862/locations/us/apps/tilicho-credit-scrapi</div></div>
   <div class="cell"><div class="l">RAG corpus (Vertex AI RAG Engine)</div><div class="v mono">projects/804472053350/locations/us-central1/ragCorpora/6713371708595634176</div></div>
   <div class="cell"><div class="l">Backend (Cloud Run)</div><div class="v mono">tilicho-credit-api-804472053350.us-central1.run.app</div></div>
   <div class="cell"><div class="l">Deployments (channels)</div><div class="v mono">api-serving (API) · web-widget (WEB_UI)</div></div>
   <div class="cell"><div class="l">Published version</div><div class="v mono">versions/6a013d7d-889c-470b-b801-42760c1fdd8d</div></div>
  </div>

  <div class="subhead">Knowledge base (RAG) &amp; guardrail</div>
  <div class="grid cols-2">
   <div class="cell"><div class="l">Knowledge base · 4 docs → Vertex AI RAG</div><div class="v" style="color:var(--lo)"><b style="color:var(--hi)">loan-terms</b> (fees, 2% foreclosure, ₹500 late fee, prepayment), <b style="color:var(--hi)">faq</b>, <b style="color:var(--hi)">fair-practices</b>, <b style="color:var(--hi)">grievance-redressal</b> (SLAs) — chunked, embedded (text-embedding-005) &amp; retrieved by the <b style="color:var(--hi)">policy_kb</b> file-search tool. The agent quotes figures from these, never world knowledge.</div></div>
   <div class="cell"><div class="l">Guardrail · compliance (LLM policy)</div><div class="v" style="color:var(--lo)">Inspects the agent's own output; blocks legal/tax/investment advice &amp; guaranteed outcomes, while permitting factual policy, fees, rates, EMIs &amp; SLAs. fail-open; on trip it replies with a safe decline + human-handoff offer.</div></div>
  </div>

  <div class="subhead">Tested — golden evals</div>
  <div class="grid">
   <div class="cell"><div class="l">Golden evals (cxas push-eval / run)</div><div class="v"><b style="color:var(--green);font-family:var(--disp);font-size:22px">6 / 6</b> behavioral scenarios pass — account lookup, payoff, grounded late-fee, hardship→restructuring, complaint-with-memory, declines-investment-advice.</div></div>
  </div>

  <div class="subhead">The code</div>
  <div class="split">
   <div>
    <div class="cell" style="border:1px solid var(--il);margin-bottom:14px">
     <div class="l">Source repo</div>
     <div class="v mono">github.com/tl-experiments/cx-lending-agent</div>
     <div class="note">Public · code-first &amp; reproducible. Key files below.</div>
    </div>
    <div class="tree">cx-lending-agent/
├─ <b>agent/</b>
│  ├─ <b>provision_scrapi.py</b>   ← CXAS SCRAPI build
│  ├─ <b>evals/goldens.yaml</b>     6 golden scenarios
│  ├─ evals/tool_tests.yaml  tool-level assertions
│  └─ scrapi_app/            pulled config (linted)
├─ <b>backend/</b>app.py          API + 3 UIs + /tour
├─ data/                  policy corpus (RAG)
├─ insights/               BigQuery analytics
└─ <b>docs/</b>EVALS.md          evals + lint writeup</div>
   </div>
   <div>
    <div class="l" style="font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--mute);margin-bottom:8px">agent/provision_scrapi.py — the SCRAPI build</div>
<pre class="code"><span class="k">from</span> cxas_scrapi <span class="k">import</span> Apps, Agents, Tools, Guardrails

t = Tools(APP_NAME)
t.create_tool(<span class="s">"servicing"</span>, <span class="s">"Servicing API"</span>,
   tool_type=<span class="s">"open_api_toolset"</span>, payload=&hellip;)  <span class="c"># REST tools</span>
t.create_tool(<span class="s">"policy-rag"</span>, <span class="s">"policy_kb"</span>,
   tool_type=<span class="s">"file_search_tool"</span>, payload=&hellip;) <span class="c"># RAG</span>
Guardrails(APP_NAME).create_guardrail(<span class="s">"compliance"</span>, &hellip;)
Agents(APP_NAME).create_agent(<span class="s">"Credit Assist"</span>,
   agent_id=<span class="s">"credit-assist"</span>, instruction=FULL)</pre>
   </div>
  </div>

  <div class="subhead">Screens</div>
  <div class="shots">
   <figure><img src="/static/img/ces-console.png" alt="CES Agent Studio console" loading="lazy" onerror="this.closest('figure').classList.add('miss')"><div class="ph">ces-console.png</div><figcaption>CES Agent Studio console · app <b>tilicho-credit-scrapi</b></figcaption></figure>
   <figure><img src="/static/img/chat.png" alt="Borrower chat" loading="lazy" onerror="this.closest('figure').classList.add('miss')"><div class="ph">chat.png</div><figcaption>Conversational agent · tools + grounding + guardrail + voice</figcaption></figure>
   <figure><img src="/static/img/insights.png" alt="Insights dashboard" loading="lazy" onerror="this.closest('figure').classList.add('miss')"><div class="ph">insights.png</div><figcaption>Conversational Insights · call-drivers + sentiment</figcaption></figure>
   <figure><img src="/static/img/cx-insights.png" alt="Native Contact Center AI Insights console" loading="lazy" onerror="this.closest('figure').classList.add('miss')"><div class="ph">cx-insights.png</div><figcaption>Native Contact Center AI Insights · sentiment + Quality AI (console)</figcaption></figure>
  </div>
  </div>
 </section>

 <section id="architecture">
  <div class="inner">
  <div class="sechead"><span class="tick"></span><h2>Architecture</h2><em>build once · runs per request</em></div>
  <div class="archd">
<div class="cp">
  <div class="cp-build">▚ built once · <b>provision_scrapi.py</b> · CXAS SCRAPI</div>
  <div class="cp-lane">
   <span class="cp-tag">CHANNEL</span>
   <div class="cp-node cp-chan">Web chat<span class="cp-sub">🎤 speak (browser speech-to-text) · 🔊 listen (Google Chirp-3 TTS) — in-chat</span></div>
  </div>
  <div class="cp-conn"><span class="cp-down">request</span><span class="cp-pkt"></span></div>
  <div class="cp-lane cp-runtime">
   <span class="cp-tag red">CES RUNTIME · GEMINI ENTERPRISE FOR CX</span>
   <div class="cp-stages">
    <button class="cp-stg active" data-i="0"><span class="cp-n">01</span>Understand</button>
    <i class="cp-arr">▸</i>
    <button class="cp-stg" data-i="1"><span class="cp-n">02</span>Ground</button>
    <i class="cp-arr">▸</i>
    <button class="cp-stg" data-i="2"><span class="cp-n">03</span>Act</button>
    <i class="cp-arr">▸</i>
    <button class="cp-stg" data-i="3"><span class="cp-n">04</span>Guard</button>
    <i class="cp-arr">▸</i>
    <button class="cp-stg" data-i="4"><span class="cp-n">05</span>Respond</button>
   </div>
   <div class="cp-detail" id="cpDetail"></div>
  </div>
  <div class="cp-conn"><span class="cp-down">tool call ▾ · grounded reply ▴</span><span class="cp-pkt"></span></div>
  <div class="cp-lane">
   <span class="cp-tag">TOOLS &amp; BACKENDS</span>
   <div class="cp-bk">
    <div class="cp-node">OpenAPI toolset → Cloud Run LMS<span class="cp-sub">actions · getAccountSummary · getPayoffQuote · createTicket</span></div>
    <div class="cp-node green">File-search → Vertex AI RAG<span class="cp-sub">grounding · policy_kb corpus (cited)</span></div>
   </div>
  </div>
  <div class="cp-tap"><span class="cp-tag green">ANALYTICS · EVERY TURN</span><span class="cp-tapflow">conversation ▸ BigQuery ▸ Contact Center AI Insights (real Google ML sentiment · entities) ▸ dashboard</span></div>
 </div>
  </div>
  <p class="note"><b style="color:var(--hi)">One control plane.</b> Every borrower turn flows through the Google-managed CES runtime — <b>Understand → Ground → Act → Guard → Respond</b> — calling our tools (actions) and Vertex RAG (grounding), then tapping every turn into Contact Center AI Insights. <b style="color:var(--hi)">Click a stage</b> to see what it does. The build plane (CXAS SCRAPI) configures it all once.</p>
  <div class="foot wrap" style="padding-left:0;padding-right:0">
   <a href="/agent" target="_blank">Live agent</a> ·
   <a href="/insights" target="_blank">Insights</a> &nbsp;—&nbsp;
   Prototype by Tilicho Labs.
  </div>
  </div>
 </section>

<script>(function(){var D=[["Understand","<b>Gemini</b> reads the borrower\u2019s turn and the CES <b>session memory</b>, then decides what\u2019s needed \u2014 a tool call, a policy lookup, or a direct reply. The loan ID + last-4 are remembered, so it never re-asks."],["Ground","For policy questions the <b>file-search tool</b> retrieves the relevant passages from the <b>Vertex AI RAG</b> corpus (loan terms, FAQ, fair-practices, grievance) \u2014 so answers are cited, not invented."],["Act","For account actions the <b>OpenAPI toolset</b> calls the lender\u2019s LMS on <b>Cloud Run</b> (getAccountSummary / getPayoffQuote / createTicket) \u2014 only after the borrower\u2019s identity (loan ID + last-4 phone) is <b>verified server-side</b> (403 on mismatch)."],["Guard","A <b>compliance guardrail</b> screens the response: it blocks legal / tax / investment advice and guaranteed outcomes, replacing them with a safe decline. An input-side jailbreak screen also runs."],["Respond","A grounded, compliant, \u20b9-formatted reply returns to the web chat. Every turn is also tapped into <b>BigQuery \u2192 Contact Center AI Insights</b> for sentiment + entities."]];var st=document.querySelectorAll(".cp-stg"),dt=document.getElementById("cpDetail");function sel(i){st.forEach(function(b,j){b.classList.toggle("active",j===i)});dt.innerHTML="<b>"+D[i][0]+".</b> "+D[i][1]}st.forEach(function(b){b.addEventListener("click",function(){sel(+b.getAttribute("data-i"))})});if(st.length)sel(0);})();</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
@app.get("/tour", response_class=HTMLResponse)
@app.get("/walkthrough", response_class=HTMLResponse)
def tour_ui():
    return _TOUR_UI
