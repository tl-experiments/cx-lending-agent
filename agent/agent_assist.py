#!/usr/bin/env python3
"""
Provision the native **Agent Assist** (Dialogflow CCAI) resources for Pillar 3:
a generative **Generator** (summary + suggested replies + grounded knowledge) and a
**ConversationProfile** that wires it. These are real, console-visible resources; the demo
/assist console calls Agent Assist's generative suggestion API against the Generator.

Generative path = no trained model, no conversation volume (unlike Smart Reply / topic models).

Usage:  python agent/agent_assist.py provision | info
Auth:   Application Default Credentials (owner). User ADC needs a quota project header.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import google.auth
from google.auth.transport.requests import Request as GAuthRequest

PROJECT = "gcex-pilot-16862"
LOC = "us"                                    # match the CES app / CX Insights region
EP = f"https://{LOC}-dialogflow.googleapis.com/v2beta1"
PARENT = f"projects/{PROJECT}/locations/{LOC}"
GENERATOR_ID = "tilicho-rep-assist"
GENERATOR_NAME = f"{PARENT}/generators/{GENERATOR_ID}"
PROFILE_DISPLAY = "Tilicho Credit Assist — Agent Assist"

# Free-form generative prompt. MUST reference ${parameter:transcript}. Output is parsed by the
# backend into [SUMMARY]/[REPLIES]/[KNOWLEDGE] for the rep console.
PROMPT = (
    "You assist a HUMAN support agent for the lender Tilicho Credit. Based on this conversation:\n"
    "${parameter:transcript}\n\n"
    "Output exactly three labelled blocks and nothing else:\n"
    "[SUMMARY] one concise line of what the customer needs.\n"
    "[REPLIES] three short, empathetic suggested agent replies, each on its own line starting with -.\n"
    "[KNOWLEDGE] the single most relevant Tilicho policy fact (late fee, foreclosure 2%, "
    "prepayment, hardship/restructuring, KYC, or grievance SLA)."
)

_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])


def _tok():
    if not _creds.valid:
        _creds.refresh(GAuthRequest())
    return _creds.token


def api(method, path, body=None):
    url = path if path.startswith("http") else f"{EP}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_tok()}")
    req.add_header("Content-Type", "application/json")
    req.add_header("x-goog-user-project", PROJECT)   # required for user ADC
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def provision():
    print(f"Agent Assist provision · project {PROJECT} · location {LOC}")
    # 1) Generator (idempotent: create, fall back to patch if it exists)
    gen_body = {
        "description": "Tilicho rep-assist: summary + suggested replies + grounded knowledge",
        "freeFormContext": {"text": PROMPT},
        "inferenceParameter": {"maxOutputTokens": 512},
        "triggerEvent": "MANUAL_CALL",
    }
    s, b = api("POST", f"{PARENT}/generators?generatorId={GENERATOR_ID}", gen_body)
    if s >= 300 and "ALREADY_EXISTS" in json.dumps(b):
        s, b = api("PATCH", f"{GENERATOR_NAME}?updateMask=freeFormContext,inferenceParameter,triggerEvent", gen_body)
        print(f"  generator: updated ({s})")
    else:
        print(f"  generator: created ({s}) {b.get('name','') or json.dumps(b)[:160]}")

    # 2) Conversation profile wiring the generator (so it shows in the Agent Assist console)
    prof_body = {
        "displayName": PROFILE_DISPLAY,
        "languageCode": "en-US",
        "humanAgentAssistantConfig": {
            "humanAgentSuggestionConfig": {"generators": [GENERATOR_NAME]}
        },
    }
    # find existing by display name (idempotent)
    s, lst = api("GET", f"{PARENT}/conversationProfiles?pageSize=200")
    existing = next((p for p in lst.get("conversationProfiles", [])
                     if p.get("displayName") == PROFILE_DISPLAY), None)
    if existing:
        s, b = api("PATCH", f"{existing['name']}?updateMask=humanAgentAssistantConfig", prof_body)
        print(f"  conversation profile: updated ({s}) {existing['name']}")
    else:
        s, b = api("POST", f"{PARENT}/conversationProfiles", prof_body)
        print(f"  conversation profile: created ({s}) {b.get('name','') or json.dumps(b)[:200]}")
    info()


def info():
    s, b = api("GET", f"{PARENT}/generators?pageSize=50")
    print("generators:", [g.get("name", "").split("/")[-1] for g in b.get("generators", [])])
    s, b = api("GET", f"{PARENT}/conversationProfiles?pageSize=50")
    print("conversation profiles:", [(p.get("displayName"), p.get("name", "").split("/")[-1])
                                      for p in b.get("conversationProfiles", [])])


if __name__ == "__main__":
    {"provision": provision, "info": info}.get(sys.argv[1] if len(sys.argv) > 1 else "info", info)()
