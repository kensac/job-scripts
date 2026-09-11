from __future__ import annotations

from typing import Any

from api import db

# Everything a person has decided, from the four logs that record it, newest
# first.
#
# Read from the logs themselves rather than from a decisions table, because
# every one of these is already append-only and a fifth copy would be the one
# that drifts. A decision that vanishes when it is reversed takes the evidence
# that the rule was wrong with it, so both survive and each says which it
# replaced.
#
# The neighbours are computed over HUMAN rows only, which is why the filter
# sits inside each branch rather than outside the window. Windowing over every
# row would point `supersedes` at the matcher's own attachment - a real row,
# but not a decision, and not in this list. Every id either side of a decision
# here resolves to another row in the same response.
#
# That the matcher can never appear as the newer row is not an accident of the
# data: `mail_match.record` refuses to overwrite a human verdict, so a human
# row's successor is always another human row.
_HISTORY_SQL = """
WITH match_decisions AS (
    SELECT am.id, am.created_at, am.actor_user_id, am.message_id, am.application_id,
           am.rationale
    FROM application_matches am
    JOIN email_messages m ON m.id = am.message_id
    WHERE m.user_id = %(user)s AND am.actor_user_id IS NOT NULL
)
SELECT 'match' AS log, d.id, d.created_at AS at, d.actor_user_id, d.application_id,
       lead(d.id) OVER (PARTITION BY d.message_id ORDER BY d.id) AS newer,
       lag(d.id) OVER (PARTITION BY d.message_id ORDER BY d.id) AS older,
       CASE WHEN d.application_id IS NOT NULL THEN 'attached' ELSE 'rejected' END AS decision,
       coalesce(a.company_name, m.subject, 'a message') AS subject
FROM match_decisions d
JOIN email_messages m ON m.id = d.message_id
LEFT JOIN applications a ON a.id = d.application_id

UNION ALL

SELECT 'proposal', sr.id, sr.created_at, sr.user_id, sr.application_id,
       lead(sr.id) OVER (PARTITION BY sr.application_id, sr.event_id ORDER BY sr.id),
       lag(sr.id) OVER (PARTITION BY sr.application_id, sr.event_id ORDER BY sr.id),
       sr.response,
       coalesce(a.company_name, 'an application')
FROM suggestion_responses sr
LEFT JOIN applications a ON a.id = sr.application_id
WHERE sr.user_id = %(user)s

UNION ALL

-- Closed by hand, not by a later email. `resolved_by_event_id` set means the
-- system settled it, which is it working rather than a decision anyone made.
--
-- No neighbours, because `action_items` is updated in place rather than
-- appended to: reopening clears `resolved_at` and the closure that preceded it
-- is gone from the row. So this log can show that an item was closed and
-- cannot show that it was closed twice. Stated rather than papered over - the
-- fix is a second table and it is not worth one for the 0 items a person has
-- ever closed.
SELECT 'action', ai.id, ai.resolved_at, ai.user_id, ai.application_id, NULL, NULL,
       'closed', coalesce(a.company_name, ai.kind)
FROM action_items ai
LEFT JOIN applications a ON a.id = ai.application_id
WHERE ai.user_id = %(user)s
  AND ai.resolved_at IS NOT NULL
  AND ai.resolved_by_event_id IS NULL

UNION ALL

SELECT 'classification', ev.id, ev.created_at, ev.actor_user_id, NULL,
       lead(ev.id) OVER (PARTITION BY ev.message_id ORDER BY ev.id),
       lag(ev.id) OVER (PARTITION BY ev.message_id ORDER BY ev.id),
       ev.kind, coalesce(m2.subject, 'a message')
FROM email_events ev
JOIN email_messages m2 ON m2.id = ev.message_id
WHERE m2.user_id = %(user)s AND ev.actor_user_id IS NOT NULL

ORDER BY at DESC, id DESC
"""


def history_for(owner_id: int, viewer_id: int, limit: int, offset: int) -> dict[str, Any]:
    rows = db.query(_HISTORY_SQL, {"user": owner_id})
    decisions = [
        {
            "id": f"{row['log']}:{row['id']}",
            "at": row["at"],
            "kind": row["log"],
            "decision": row["decision"],
            # The same actor id reads as "you" to the owner and as an
            # administrator to anyone else, derived rather than stored twice.
            "by": "you" if row["actor_user_id"] == viewer_id else "administrator",
            "summary": row["subject"],
            "application_id": row["application_id"],
            "superseded_by": f"{row['log']}:{row['newer']}" if row["newer"] else None,
            "supersedes": f"{row['log']}:{row['older']}" if row["older"] else None,
        }
        for row in rows
    ]
    return {"decisions": decisions[offset : offset + limit], "total": len(decisions)}
