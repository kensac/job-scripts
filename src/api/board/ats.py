"""The applicant tracking system a posting's url lives on."""

from __future__ import annotations

from typing import Literal

# The host names the ATS for the hosted ones; the rest fold into "other",
# because on a real board the tail is wide (183 employer careers hosts against
# five systems on 2026-09-07) and a select of 188 entries filters nothing.
AtsName = Literal[
    "ashby",
    "greenhouse",
    "lever",
    "workable",
    "workday",
    "smartrecruiters",
    "icims",
    "jobvite",
    "bamboohr",
    "rippling",
    "gem",
    "other",
]

# The applicant tracking system a posting's url lives on, as one definition
# used by the row, the filter and the options. The patterns and the names are
# one table so a system added to the SQL cannot be missing from the type that
# declares what the column can hold, which is how a Literal turns into a 500.
_ATS_PATTERNS: dict[AtsName, str] = {
    "ashby": "https://jobs.ashbyhq.com/%%",
    "greenhouse": "%%greenhouse.io/%%",
    "lever": "https://jobs.lever.co/%%",
    "workable": "%%workable.com/%%",
    "workday": "%%myworkdayjobs.com/%%",
    "smartrecruiters": "%%smartrecruiters.com/%%",
    "icims": "%%icims.com/%%",
    "jobvite": "%%jobvite.com/%%",
    "bamboohr": "%%bamboohr.com/%%",
    "rippling": "%%rippling.com/%%",
    "gem": "https://jobs.gem.com/%%",
}

ATS_SQL = (
    "\n    CASE\n"
    + "".join(
        f"      WHEN j.url ILIKE '{pattern}' THEN '{name}'\n"
        for name, pattern in _ATS_PATTERNS.items()
    )
    + "      ELSE 'other'\n    END\n"
)
