from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.board import visibility

router = APIRouter()


# The rows the person sees, with their own board fields beside them. Every
# count below reads this rather than user_jobs, because a user_jobs row is
# not the board: the worker materialises one for every posting that ever
# passed, and the row outlives the posting's membership. Counted from
# user_jobs, "to apply" read 2,331 on 2026-09-07 while the board held 578,
# because 1,753 untouched rows carried postings the filters had since
# rejected or the person's expiry had aged out.
_BOARD = visibility.FAST.format(
    columns="j.id, j.source, uj.status, uj.date_applied, uj.hidden",
    extra="",
)


class StatusCount(BaseModel):
    """A status and how many board rows carry it. The empty string is the
    board's own name for "no status yet", not a missing value, which is why
    the query coalesces rather than letting null through."""

    status: str
    count: int


class SourceCounts(BaseModel):
    source: str
    total: int
    with_status: int
    applied: int


class SourceStatusCount(BaseModel):
    source: str
    status: str
    count: int


class AppliedWeek(BaseModel):
    week: datetime.date
    applied: int


class Totals(BaseModel):
    """The bookkeeping rows, deliberately not the board. Counted from
    user_jobs, so `tracked` includes rows whose posting has since left."""

    tracked: int
    applied: int
    hidden: int


class Stats(BaseModel):
    totals: Totals
    by_status: list[StatusCount]
    by_source: list[SourceCounts]
    by_source_status: list[SourceStatusCount]
    applied_by_week: list[AppliedWeek]


@router.get("/user/stats")
def stats(user: AuthedUser = Depends(require_user)) -> Stats:
    params = {"uid": user.id}
    by_status = db.query_as(
        StatusCount,
        f"""
        SELECT COALESCE(v.status, '') AS status, COUNT(*) AS count
        FROM ({_BOARD}) v WHERE NOT COALESCE(v.hidden, FALSE)
        GROUP BY 1 ORDER BY count DESC
        """,
        params,
    )
    by_source = db.query_as(
        SourceCounts,
        f"""
        SELECT v.source, COUNT(*) AS total,
               COUNT(*) FILTER (WHERE v.status IS NOT NULL AND v.status != '') AS with_status,
               COUNT(*) FILTER (WHERE v.date_applied IS NOT NULL) AS applied
        FROM ({_BOARD}) v WHERE NOT COALESCE(v.hidden, FALSE)
        GROUP BY v.source ORDER BY total DESC
        """,
        params,
    )
    by_source_status = db.query_as(
        SourceStatusCount,
        f"""
        SELECT v.source, COALESCE(v.status, '') AS status, COUNT(*) AS count
        FROM ({_BOARD}) v WHERE NOT COALESCE(v.hidden, FALSE)
        GROUP BY v.source, v.status ORDER BY v.source, count DESC
        """,
        params,
    )
    over_time = db.query_as(
        AppliedWeek,
        "SELECT date_trunc('week', date_applied)::date AS week, COUNT(*) AS applied "
        "FROM user_jobs WHERE user_id = %s AND date_applied IS NOT NULL "
        "GROUP BY week ORDER BY week",
        (user.id,),
    )
    totals = db.query_one_as(
        Totals,
        """
        SELECT COUNT(*) AS tracked,
               COUNT(*) FILTER (WHERE date_applied IS NOT NULL) AS applied,
               COUNT(*) FILTER (WHERE hidden) AS hidden
        FROM user_jobs WHERE user_id = %s
        """,
        (user.id,),
    )
    # An aggregate with no GROUP BY returns its row even for a person with
    # nothing tracked.
    assert totals
    return Stats(
        totals=totals,
        by_status=by_status,
        by_source=by_source,
        by_source_status=by_source_status,
        applied_by_week=over_time,
    )


# Reaching a stage means an event of that kind ever arrived for the
# application, not that it is sitting there now - an application that was
# acknowledged and then rejected counts in both. That is what makes the
# numbers a funnel rather than a snapshot, and it is why they do not sum to
# the total.
_FUNNEL_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("acknowledged", ("acknowledgement",)),
    ("assessment", ("assessment_invite",)),
    ("interview", ("interview_invite", "interview_scheduled")),
    ("rejected", ("rejection",)),
    ("closed_by_employer", ("position_closed",)),
)

# `offer` is deliberately absent. 71 applications reach it against 53 reaching
# interview_invite, which is backwards on its face and is the club-acceptance
# misclassification showing through: student-organisation decisions and Model
# UN allocations currently classify as offers. A funnel reading "more offers
# than interviews" would discredit every other number beside it, so the stage
# is omitted and the omission is stated in the response rather than hidden.
_EXCLUDED_STAGES = {
    "offer": (
        "excluded pending a reclassification: some student-organisation and "
        "programme acceptances currently classify as job offers, which would "
        "make this stage read higher than interviews"
    )
}

# Below this a per-source rate is noise: the conventional floor for a binomial
# proportion, and the same one the board analytics use. Most sources sit far
# under it today because applications are only created when a tracked posting
# is marked applied, and that has mostly happened for one source.
_MIN_SOURCE_SAMPLE = 30

_FUNNEL_SQL = """
WITH ev AS (
    SELECT DISTINCT am.application_id, e.kind
    FROM email_events e
    JOIN application_matches am ON am.message_id = e.message_id
    WHERE am.application_id IS NOT NULL
)
SELECT ap.id, j.source, array_agg(DISTINCT ev.kind) FILTER (WHERE ev.kind IS NOT NULL) AS kinds
FROM applications ap
LEFT JOIN jobs j ON j.id = ap.job_id
LEFT JOIN ev ON ev.application_id = ap.id
WHERE ap.user_id = %s AND ap.dismissed_at IS NULL
GROUP BY ap.id, j.source
"""


def _stage_counts(rows: list[dict]) -> dict[str, int]:
    counts = dict.fromkeys((name for name, _ in _FUNNEL_STAGES), 0)
    for row in rows:
        kinds = set(row["kinds"] or ())
        for name, triggers in _FUNNEL_STAGES:
            if kinds.intersection(triggers):
                counts[name] += 1
    return counts


def _funnel(rows: list[dict], min_sample: int) -> dict[str, Any]:
    total = len(rows)
    counts = _stage_counts(rows)
    return {
        "applications": total,
        "stages": [
            {"stage": name, "reached": counts[name], "of": total} for name, _ in _FUNNEL_STAGES
        ],
        "below_sample_floor": total < min_sample,
    }


class FunnelStage(BaseModel):
    """One stage with its own denominator. `reached` counts applications an
    event of that kind ever arrived for, so the stages overlap and do not sum
    to `of`."""

    stage: str
    reached: int
    of: int


class Funnel(BaseModel):
    applications: int
    stages: list[FunnelStage]
    below_sample_floor: bool


class SourceFunnel(Funnel):
    source: str


class ExcludedStage(BaseModel):
    """A stage left out, said out loud. A number quietly missing is worse
    than one that names why it is not here."""

    stage: str
    reason: str


class FunnelReport(BaseModel):
    overall: Funnel
    by_source: list[SourceFunnel]
    excluded_stages: list[ExcludedStage]
    min_sample: int


@router.get("/user/funnel")
def funnel(user: AuthedUser = Depends(require_user)) -> FunnelReport:
    """What happened to the applications, as counts with their denominator.

    Every stage ships `reached` and `of` rather than a percentage, because the
    prose that matters is "299 of 714", not "42%".

    The per-source breakdown is the one that decides whether a board is worth
    keeping - volume and survival say nothing about whether a source has ever
    produced an interview. It is thin today by construction: an application is
    created when a TRACKED posting is marked applied, and that has happened for
    essentially one source, so the rest report their real n and are flagged
    below the floor rather than rendered as zero rates.
    """
    rows = db.query(_FUNNEL_SQL, (user.id,))
    by_source: dict[str, list[dict]] = {}
    for row in rows:
        by_source.setdefault(row["source"] or "(not from a tracked posting)", []).append(row)
    return FunnelReport(
        overall=Funnel(**_funnel(rows, _MIN_SOURCE_SAMPLE)),
        by_source=sorted(
            (
                SourceFunnel(source=source, **_funnel(source_rows, _MIN_SOURCE_SAMPLE))
                for source, source_rows in by_source.items()
            ),
            key=lambda entry: -entry.applications,
        ),
        excluded_stages=[
            ExcludedStage(stage=stage, reason=reason) for stage, reason in _EXCLUDED_STAGES.items()
        ],
        min_sample=_MIN_SOURCE_SAMPLE,
    )
