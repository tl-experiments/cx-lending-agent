-- Conversational Insights: call-driver distribution from the CES BigQuery export.
-- Source table is the CES conversation export (dataset tilicho_cx_insights).
-- Classifies each conversation by the borrower's question into a "call driver"
-- (the top reasons customers contact us) — the core CX-ops KPI.
WITH user_msgs AS (
  SELECT conversation_id,
    LOWER((SELECT STRING_AGG(JSON_VALUE(c,"$.text")," ")
           FROM UNNEST(JSON_QUERY_ARRAY(m.chunks)) c
           WHERE JSON_VALUE(c,"$.text") IS NOT NULL)) AS q
  FROM `gcex-pilot-16862.tilicho_cx_insights.tilicho-credit`, UNNEST(messages) m
  WHERE m.role = "user"
),
classified AS (
  SELECT conversation_id,
    CASE
      WHEN REGEXP_CONTAINS(q, r"foreclos|payoff")                         THEN "Foreclosure"
      WHEN REGEXP_CONTAINS(q, r"kyc")                                      THEN "KYC update"
      WHEN REGEXP_CONTAINS(q, r"complain|wrongly|frustrat|unhappy")        THEN "Complaint"
      WHEN REGEXP_CONTAINS(q, r"lost.*job|can.?.?t pay|hardship|restructur") THEN "Hardship"
      WHEN REGEXP_CONTAINS(q, r"fee|charge|prepay|policy|penalty")          THEN "Policy/Fees"
      WHEN REGEXP_CONTAINS(q, r"emi|balance|due|outstanding")              THEN "Account/EMI"
      ELSE "Other" END AS call_driver
  FROM user_msgs WHERE q IS NOT NULL
)
SELECT call_driver,
       COUNT(DISTINCT conversation_id) AS conversations,
       ROUND(100 * COUNT(DISTINCT conversation_id) / SUM(COUNT(DISTINCT conversation_id)) OVER (), 1) AS pct
FROM classified
GROUP BY call_driver
ORDER BY conversations DESC;
