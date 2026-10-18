"""Shared structural eligibility for board computation and filter work.

Source admission and custom verdict requirements belong to each caller:
materialization also accepts sheet imports and requires an enabled filter,
while visibility grants uploads and acted-on rows independently of these gates.
"""

from typing import Any

from api import user_settings
from api.board import criteria
from core import catalog, verdict_reads
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

STRUCTURAL = f"""
        {catalog.IS_AVAILABLE.format(job="j")}
        {{criteria}}
        AND EXISTS (SELECT 1 FROM latest_check lc
                    WHERE lc.url = j.url AND lc.check_type = 'closed' AND lc.status = 'passed')
        AND (%(bypass_sponsorship)s
             OR EXISTS (SELECT 1 FROM latest_check lc
                        WHERE lc.url = j.url AND lc.check_type = 'clearance' AND lc.status = 'passed'))
"""


# STRUCTURAL with each posting's latest closed and clearance answers looked up
# on idx_ai_queries_latest_verdict instead of from latest_check, which reads
# the latest answer of every url first. For a board's sources that is the
# faster shape: on production 2026-10-18, board 2's candidates (25,546 rows)
# took 13.9 s against 38.1 s, and from oci the old shape held the worker's
# main thread past its heartbeat. A person's FULL read spans every source
# they follow and is slower this way (31-38 s against 53-57 s), so it keeps
# STRUCTURAL.
STRUCTURAL_PER_POSTING = f"""
        {catalog.IS_AVAILABLE.format(job="j")}
        {{criteria}}
        AND {verdict_reads.latest_status("j.url", "closed")} = 'passed'
        AND (%(bypass_sponsorship)s
             OR {verdict_reads.latest_status("j.url", "clearance")} = 'passed')
"""


def settings_params(user_id: int) -> dict[str, Any]:
    settings = user_settings.criteria(user_id)
    return {
        "uid": user_id,
        "bypass_sponsorship": settings.bypass_sponsorship_filter,
        **criteria.params({"criteria": settings.criteria}),
    }
