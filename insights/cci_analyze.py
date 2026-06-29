#!/usr/bin/env python3
"""
Pull the REAL Contact Center AI Insights (CCAI) analysis into BigQuery so the demo /insights
dashboard can surface it.

Gemini Enterprise for CX auto-ingests the CES app's conversations into CCAI (location `us`) and
analyses them. This job READS those pre-computed results — per-conversation Google ML sentiment
(SENTIMENT_MODEL_TYPE_V2) + entities — and writes them to BigQuery table `cci_insights`. It does
NOT upload conversations or run its own Cloud NL pass; the analysis is CCAI's own.

(It first triggers conversations:bulkAnalyze so freshly-captured conversations get analysed.)

Usage:  python insights/cci_analyze.py
Auth:   Application Default Credentials (a principal with contactcenterinsights read + BigQuery).
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import google.auth
from google.auth.transport.requests import Request as GAuthRequest

PROJECT = "gcex-pilot-16862"
LOC = "us"                                   # CCAI location must match the CES app's region
CCAI = f"https://{LOC}-contactcenterinsights.googleapis.com/v1"   # regional endpoint for `us`
PARENT = f"projects/{PROJECT}/locations/{LOC}"
BQ_TABLE = f"{PROJECT}.tilicho_cx_insights.cci_insights"
# Only the LIVE app — CCAI also retains conversations from deleted apps (it's an archive), so we
# filter by agentId to keep the dashboard scoped to the current SCRAPI build.
AGENT_ID = "tilicho-credit-scrapi"
FILTER = f'agent_id="{AGENT_ID}"'

_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])


def _tok():
    if not _creds.valid:
        _creds.refresh(GAuthRequest())
    return _creds.token


def api(base, method, url, body=None):
    full = url if url.startswith("http") else f"{base}/{url}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(full, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_tok()}")
    req.add_header("Content-Type", "application/json")
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


# Drop noise from CCAI's entity extraction: loan-id tokens (TL-1001), verification artifacts,
# and leftovers from the old injected context — keep only meaningful CX topics.
_ENTITY_STOP = {"instructions", "system", "context", "number", "digits"}


def keep_entity(name):
    n = (name or "").strip().lower()
    if len(n) <= 2:                       # "tl", single letters
        return False
    if re.match(r"^tl-?\d*$", n):         # loan ids like tl-1001
        return False
    if re.fullmatch(r"[\d\W]+", n):       # pure numbers / punctuation
        return False
    return n not in _ENTITY_STOP


def label(score):
    if score is None:
        return "n/a"
    return "positive" if score > 0.2 else "negative" if score < -0.2 else "neutral"


def _sql_str(s):
    return "'" + (s or "").replace("\\", "\\\\").replace("'", "''").replace("\n", " ") + "'"


def main():
    # 1) Make sure freshly-captured conversations are analysed (idempotent; analysis is CCAI's ML).
    s, op = api(CCAI, "POST", f"{PARENT}/conversations:bulkAnalyze",
                {"filter": FILTER, "analysisPercentage": 100})
    print(f"bulkAnalyze: {s} (async; existing analyses are reused)")
    opn = op.get("name", "")
    for _ in range(40):
        if not opn:
            break
        _, st = api(CCAI, "GET", opn)
        if st.get("done"):
            break
        time.sleep(3)

    # 2) Read CCAI's own analysis results (sentiment + entities) per conversation.
    rows, tok = [], ""
    while True:
        s, b = api(CCAI, "GET",
                   f"{PARENT}/conversations?pageSize=100&view=FULL&filter={urllib.parse.quote(FILTER)}"
                   + (f"&pageToken={tok}" if tok else ""))
        for c in b.get("conversations", []):
            cid = c.get("name", "").split("/")[-1]
            cam = (c.get("latestAnalysis", {}).get("analysisResult", {})
                   .get("callAnalysisMetadata", {}))
            scores = [x.get("sentimentData", {}).get("score") for x in cam.get("sentiments", [])
                      if x.get("sentimentData", {}).get("score") is not None]
            if not scores:
                continue  # only surface conversations CCAI has actually scored
            avg = round(sum(scores) / len(scores), 3)
            ents = sorted((cam.get("entities") or {}).values(),
                          key=lambda e: e.get("salience", 0), reverse=True)
            names = [e.get("displayName") for e in ents
                     if e.get("displayName") and keep_entity(e.get("displayName"))][:5]
            rows.append({"conversation_id": cid, "sentiment": label(avg),
                         "sentiment_score": avg, "entities": ", ".join(names)})
        tok = b.get("nextPageToken", "")
        if not tok:
            break

    if not rows:
        print("No CCAI-scored conversations found yet (analysis may still be running).")
        return

    # 3) Write to BigQuery atomically (CREATE OR REPLACE — no streaming buffer / dedup issues).
    values = ",\n".join(
        f"STRUCT({_sql_str(r['conversation_id'])} AS conversation_id, "
        f"{_sql_str(r['sentiment'])} AS sentiment, {r['sentiment_score']} AS sentiment_score, "
        f"{_sql_str(r['entities'])} AS entities)" for r in rows)
    sql = f"CREATE OR REPLACE TABLE `{BQ_TABLE}` AS SELECT * FROM UNNEST([{values}])"
    s, b = api("https://bigquery.googleapis.com/bigquery/v2",
               "POST", f"projects/{PROJECT}/queries",
               {"query": sql, "useLegacySql": False, "timeoutMs": 60000})
    ok = s < 300
    print(f"BigQuery write: {s} {'ok (' + str(len(rows)) + ' rows)' if ok else json.dumps(b)[:200]}")

    pos = sum(1 for r in rows if r["sentiment"] == "positive")
    neu = sum(1 for r in rows if r["sentiment"] == "neutral")
    neg = sum(1 for r in rows if r["sentiment"] == "negative")
    print(f"CCAI sentiment — positive:{pos} neutral:{neu} negative:{neg}  (model SENTIMENT_MODEL_TYPE_V2)")


if __name__ == "__main__":
    main()
