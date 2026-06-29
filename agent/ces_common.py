#!/usr/bin/env python3
"""
Shared CES helpers for the SCRAPI provisioner: config loaded from infra/.env, the OpenAPI
tool schema, the tool ids, and a thin authenticated REST client (used for the one PATCH the
SCRAPI SDK can't express — refreshing a toolset's OpenAPI schema). Uses Application Default
Credentials.
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

import google.auth
from google.auth.transport.requests import Request as GAuthRequest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Config: infra/.env if present (gitignored), else infra/env.example as a template, with
# os.environ taking precedence. Required keys must be real (not the example placeholders);
# otherwise exit with a friendly message instead of a traceback — so a fresh clone runs cleanly.
ENV = {}
for _name in ("infra/env.example", "infra/.env"):   # .env overrides the example
    _p = ROOT / _name
    if _p.exists():
        for line in _p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                ENV[k.strip()] = v.split("#", 1)[0].strip()
for _k in ("PROJECT_ID", "PROJECT_NUMBER", "CES_LOCATION", "BACKEND_URL", "RAG_CORPUS", "BACKEND_API_KEY"):
    if os.environ.get(_k):
        ENV[_k] = os.environ[_k]

_PLACEHOLDERS = {"your-project-id", "000000000000", ""}
_missing = [k for k in ("PROJECT_ID", "PROJECT_NUMBER", "BACKEND_URL")
            if ENV.get(k, "") in _PLACEHOLDERS]
if _missing:
    sys.exit("Configuration needed: copy infra/env.example -> infra/.env and set "
             "PROJECT_ID, PROJECT_NUMBER, BACKEND_URL (and RAG_CORPUS). "
             f"Missing/placeholder: {', '.join(_missing)}.")

PROJECT_ID = ENV["PROJECT_ID"]
PROJECT_NUMBER = ENV["PROJECT_NUMBER"]
LOCATION = ENV.get("CES_LOCATION", "us")
BACKEND_URL = ENV["BACKEND_URL"]
SECRET_VERSION = f"projects/{PROJECT_NUMBER}/secrets/tilicho-backend-api-key/versions/latest"

BASE = "https://ces.googleapis.com/v1beta"
PARENT = f"projects/{PROJECT_ID}/locations/{LOCATION}"

# Tool ids = the OpenAPI operationIds the toolset generates. MUST be listed explicitly in the
# agent's toolset reference — an empty toolIds exposes NO tools to the planner.
TOOL_IDS = ["getAccountSummary", "getPayoffQuote", "createTicket"]

_creds = None


def _token():
    global _creds
    if _creds is None:
        _creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not _creds.valid:
        _creds.refresh(GAuthRequest())
    return _creds.token


def api(method, url, body=None):
    """Authenticated CES REST call. Returns (status, parsed_json)."""
    if url.startswith("projects/"):
        url = f"{BASE}/{url}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def load_openapi_schema():
    return (ROOT / "agent" / "tool-openapi-3.0.yaml").read_text()
