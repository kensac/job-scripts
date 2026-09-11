from __future__ import annotations

from typing import Any

from api import db

# Confirm and reject rates per tier. The GROUPING IS THE POINT: "is the matcher
# right" is unanswerable, while "is ats_company at medium confidence right"
# decides whether that tier should keep writing unattended. It only works
# because confirming preserves the method the matcher wrote instead of
# restamping it `manual`.
#
# Rejections are counted against the tier that MADE the attachment, which is
# the row underneath the rejection rather than the rejection itself - a
# `detached` row names no tier, so counting by its own method would put every
# rejection in one bucket and no tier would ever look wrong.
_REVIEW_RATES_SQL = """
WITH ranked AS (
    SELECT am.id, am.message_id, am.application_id, am.method, am.confidence,
           am.actor_user_id,
           row_number() OVER (PARTITION BY am.message_id ORDER BY am.id DESC) AS rn,
           lag(am.method) OVER (PARTITION BY am.message_id ORDER BY am.id) AS prev_method,
           lag(am.confidence) OVER (PARTITION BY am.message_id ORDER BY am.id) AS prev_confidence,
           lag(am.application_id) OVER (PARTITION BY am.message_id ORDER BY am.id) AS prev_app,
           lag(am.actor_user_id) OVER (PARTITION BY am.message_id ORDER BY am.id) AS prev_actor
    FROM application_matches am
    JOIN email_messages m ON m.id = am.message_id
    -- NULL means every user, which is what /job-scripts is: the view across
    -- the fleet, not one person's data behind a permission level.
    WHERE (%(user)s::bigint IS NULL OR m.user_id = %(user)s)
),
attached AS (
    SELECT method, confidence,
           count(*) AS attached,
           count(*) FILTER (WHERE actor_user_id IS NOT NULL) AS reviewed
    FROM ranked WHERE rn = 1 AND application_id IS NOT NULL
    GROUP BY 1, 2
),
answered AS (
    -- A human row whose predecessor was the matcher's. Same application means
    -- they agreed; a NULL application means they threw it out.
    SELECT prev_method AS method, prev_confidence AS confidence,
           count(*) FILTER (WHERE application_id IS NOT DISTINCT FROM prev_app) AS confirmed,
           count(*) FILTER (WHERE application_id IS NULL AND prev_app IS NOT NULL) AS rejected
    FROM ranked
    WHERE actor_user_id IS NOT NULL AND prev_actor IS NULL AND prev_app IS NOT NULL
    GROUP BY 1, 2
)
SELECT coalesce(att.method, ans.method) AS method,
       coalesce(att.confidence, ans.confidence) AS confidence,
       coalesce(att.attached, 0) AS attached,
       coalesce(att.reviewed, 0) AS reviewed,
       coalesce(ans.confirmed, 0) AS confirmed,
       coalesce(ans.rejected, 0) AS rejected
FROM attached att
FULL OUTER JOIN answered ans
  ON ans.method = att.method AND ans.confidence IS NOT DISTINCT FROM att.confidence
ORDER BY 3 DESC, 1
"""


def review_rates_for(user_id: int | None) -> dict[str, Any]:
    rows = db.query(_REVIEW_RATES_SQL, {"user": user_id})
    by_method = []
    for row in rows:
        answered = row["confirmed"] + row["rejected"]
        by_method.append(
            {
                **row,
                "confirm_rate": (row["confirmed"] / answered) if answered else None,
                "note": None if answered else "not measured: nobody has reviewed this tier yet",
            }
        )
    return {
        "by_method": by_method,
        "never_reviewed": sum(r["attached"] - r["reviewed"] for r in rows),
        "reviewed": sum(r["reviewed"] for r in rows),
    }
