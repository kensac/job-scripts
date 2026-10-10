"""Shared structural eligibility for board computation and filter work.

Source admission and custom verdict requirements belong to each caller:
materialization also accepts sheet imports and requires an enabled filter,
while visibility grants uploads and acted-on rows independently of these gates.
"""

from typing import Any

from api import user_settings
from api.board import criteria
from core import verdict_reads
from core.store import PERSON_SOURCE

SUBSCRIBED = PERSON_SOURCE.format(source="j.source", user="%(uid)s")

# Verdicts are keyed by prompt hash; duplicate filters must not multiply the
# number of passes needed when reading legacy rows with the same prompt.
ENABLED_FILTERS = """
SELECT DISTINCT prompt_hash FROM user_filters WHERE user_id = %(uid)s AND enabled
"""

_LATEST_CHECKS = verdict_reads.latest_per(
    "url, check_type", "url, check_type, status", "check_type IN ('closed', 'clearance')"
)

LATEST_CHECK = f"""
latest_check AS (
    {_LATEST_CHECKS}
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
    settings = user_settings.criteria(user_id)
    return {
        "uid": user_id,
        "bypass_sponsorship": settings.bypass_sponsorship_filter,
        **criteria.params({"criteria": settings.criteria}),
    }
