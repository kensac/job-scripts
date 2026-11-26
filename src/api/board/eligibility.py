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

# Membership in latest_check's passing urls, as a set built once per
# statement and probed per posting. `IS TRUE` keeps it a hashed subplan; as an
# EXISTS (or a bare IN) at the top of a WHERE the planner turns it into a
# join driven from latest_check, one jobs lookup per passing url: 788,476 of
# them, 22 of a person's 24-second board read on a production copy
# (2026-11-26).
_PASSED = """(j.url IN (SELECT lc.url FROM latest_check lc
                    WHERE lc.check_type = '{check}' AND lc.status = 'passed')) IS TRUE"""

STRUCTURAL = f"""
        {catalog.IS_AVAILABLE.format(job="j")}
        {{criteria}}
        AND {_PASSED.format(check="closed")}
        AND (%(bypass_sponsorship)s OR {_PASSED.format(check="clearance")})
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
