# Conversational Insights (Pillar 4)

How we turn live agent conversations into CX analytics.

## Pipeline
```
CES agent conversations
   ├─ (loggingSettings.bigqueryExportSettings.enabled=true)
   │     ▼  BigQuery  tilicho_cx_insights.`tilicho-credit-scrapi`  →  call drivers + transcripts (SQL, this folder)
   │
   └─ (auto-ingest) ▼  Contact Center AI Insights (native, location us)
                         │  Google ML: sentiment (V2) + entities
                         ▼  cci_analyze.py PULLS it → BigQuery `cci_insights`
                            → surfaced on the /insights dashboard
```

Both feeds are wired by `agent/provision_scrapi.py` (BigQuery export) + the platform's
automatic CCAI ingestion. `insights/cci_analyze.py` reads CCAI's **own** analysis (it does not
run a separate sentiment pass) and writes sentiment + entities to `cci_insights`.

## Run it
```sh
bash insights/query.sh
```
- **call_drivers.sql** — classifies each conversation by the borrower's question into
  a call driver (Foreclosure, Account/EMI, KYC, Complaint, Hardship, Policy/Fees) and
  shows volume + %. This is the headline CX-ops KPI ("what are customers calling
  about?").
- **transcripts.sql** — borrower question + agent reply per conversation, for QA review.

## Sample result (from real exported demo conversations)
| call_driver | conversations |
|---|---|
| Foreclosure | 2 |
| Account/EMI | 1 |
| KYC update | 1 |
| Complaint | 1 |
| Hardship | 1 |

## Native Contact Center AI Insights — what's wired vs next
- **Done — sentiment + entities:** pulled from the native CCAI product (`cci_analyze.py` →
  `cci_insights`) and shown on the dashboard. Real Google ML (sentiment model V2), not ours.
- **Next layer (need volume):** **topic modeling** requires a trained issue model + ~1000+
  conversations; **Quality-AI** needs scorecards. Both run inside the native CX Insights console
  on the same auto-ingested conversations — not built at this demo's volume.

> Demo talking point: every conversation is captured and analyzable automatically —
> 100% coverage, not a sampled QA process.
