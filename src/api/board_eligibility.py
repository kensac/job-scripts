"""Shared structural eligibility for board computation and filter work.

Source admission and custom verdict requirements belong to each caller:
materialization also accepts sheet imports and requires an enabled filter,
while visibility grants uploads and acted-on rows independently of these gates.
"""

from typing import Any

from api import criteria, db

SUBSCRIBED = "j.source IN (SELECT source FROM user_sources WHERE user_id = %(uid)s)"

# Verdicts are keyed by prompt hash; duplicate filters must not multiply the
# number of passes needed when reading legacy rows with the same prompt.
ENABLED_FILTERS = """
SELECT DISTINCT prompt_hash FROM user_filters WHERE user_id = %(uid)s AND enabled
"""

LATEST_CHECK = """
latest_check AS (
    SELECT DISTINCT ON (url, check_type) url, check_type, status
    FROM ai_queries
    WHERE check_type IN ('closed', 'clearance') AND status IN ('passed', 'rejected')
    ORDER BY url, check_type, id DESC
)
"""

STRUCTURAL = """
        j.active
        {criteria}
        AND EXISTS (SELECT 1 FROM latest_check lc
                    WHERE lc.url = j.url AND lc.check_type = 'closed' AND lc.status = 'passed')
        AND (%(bypass_sponsorship)s
             OR EXISTS (SELECT 1 FROM latest_check lc
                        WHERE lc.url = j.url AND lc.check_type = 'clearance' AND lc.status = 'passed'))
"""


def settings_params(user_id: int) -> dict[str, Any]:
    settings = db.query_one(
        "SELECT bypass_sponsorship_filter, criteria FROM user_settings WHERE user_id = %s",
        (user_id,),
    )
    return {
        "uid": user_id,
        "bypass_sponsorship": settings["bypass_sponsorship_filter"] if settings else True,
        **criteria.params(settings),
    }
