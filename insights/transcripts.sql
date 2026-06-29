-- Conversational Insights: readable transcript per conversation (borrower question
-- + agent reply), from the CES BigQuery export. Useful for QA review / spot-checks.
WITH turns AS (
  SELECT conversation_id, m.role AS role, m.event_time AS t,
    (SELECT STRING_AGG(JSON_VALUE(c,"$.text")," ")
     FROM UNNEST(JSON_QUERY_ARRAY(m.chunks)) c
     WHERE JSON_VALUE(c,"$.text") IS NOT NULL) AS text
  FROM `gcex-pilot-16862.tilicho_cx_insights.tilicho-credit`, UNNEST(messages) m
)
SELECT conversation_id,
  STRING_AGG(IF(role="user", text, NULL), " ") AS borrower_said,
  STRING_AGG(IF(role!="user", text, NULL), " ") AS agent_replied
FROM turns
WHERE text IS NOT NULL AND text != ""
GROUP BY conversation_id
ORDER BY conversation_id;
