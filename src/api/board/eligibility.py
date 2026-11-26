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

# Available, inside the criteria, and its latest closed and clearance answers
# passed, each looked up per posting on idx_ai_queries_latest_verdict. Read
# per posting, the cost follows the candidates that reach the lookup, after
# the cheap conditions. The other shape built the latest answer of every url
# first and probed it as a set. It was faster only while the planner hashed
# that set, which it did only because it estimated 9 passing urls against
# 788,476 (production copy, 2026-11-26); estimated right, the set outgrows
# hash memory (work_mem x hash_mem_multiplier) and every posting rescans it.
# Measured on that copy: board 2's candidates 13.9 s against 38.1 s
# (production 2026-10-18), and a person's board with no filter 15.4 s against
# 35.7 s on main.
STRUCTURAL = f"""
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
