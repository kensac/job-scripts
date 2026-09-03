from __future__ import annotations

from fastapi import APIRouter, Depends

from api import db
from api.auth import AuthedUser, require_user

router = APIRouter()


@router.get("/user/stats")
def stats(user: AuthedUser = Depends(require_user)):
    by_status = db.query(
        "SELECT COALESCE(status, '') AS status, COUNT(*) AS count "
        "FROM user_jobs WHERE user_id = %s AND NOT hidden "
        "GROUP BY status ORDER BY count DESC",
        (user.id,),
    )
    by_source = db.query(
        """
        SELECT j.source, COUNT(*) AS total,
               COUNT(*) FILTER (WHERE uj.status IS NOT NULL AND uj.status != '') AS with_status,
               COUNT(*) FILTER (WHERE uj.date_applied IS NOT NULL) AS applied
        FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
        WHERE uj.user_id = %s AND NOT uj.hidden
        GROUP BY j.source ORDER BY total DESC
        """,
        (user.id,),
    )
    by_source_status = db.query(
        """
        SELECT j.source, COALESCE(uj.status, '') AS status, COUNT(*) AS count
        FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
        WHERE uj.user_id = %s AND NOT uj.hidden
        GROUP BY j.source, uj.status ORDER BY j.source, count DESC
        """,
        (user.id,),
    )
    over_time = db.query(
        "SELECT date_trunc('week', date_applied)::date AS week, COUNT(*) AS applied "
        "FROM user_jobs WHERE user_id = %s AND date_applied IS NOT NULL "
        "GROUP BY week ORDER BY week",
        (user.id,),
    )
    totals = db.query_one(
        """
        SELECT COUNT(*) AS tracked,
               COUNT(*) FILTER (WHERE date_applied IS NOT NULL) AS applied,
               COUNT(*) FILTER (WHERE hidden) AS hidden
        FROM user_jobs WHERE user_id = %s
        """,
        (user.id,),
    )
    return {
        "totals": totals,
        "by_status": by_status,
        "by_source": by_source,
        "by_source_status": by_source_status,
        "applied_by_week": over_time,
    }


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


def _funnel(rows: list[dict], min_sample: int) -> dict:
    total = len(rows)
    counts = _stage_counts(rows)
    return {
        "applications": total,
        "stages": [
            {"stage": name, "reached": counts[name], "of": total} for name, _ in _FUNNEL_STAGES
        ],
        "below_sample_floor": total < min_sample,
    }


@router.get("/user/funnel")
def funnel(user: AuthedUser = Depends(require_user)):
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
    return {
        "overall": _funnel(rows, _MIN_SOURCE_SAMPLE),
        "by_source": sorted(
            (
                {"source": source, **_funnel(source_rows, _MIN_SOURCE_SAMPLE)}
                for source, source_rows in by_source.items()
            ),
            key=lambda entry: -entry["applications"],
        ),
        "excluded_stages": [
            {"stage": stage, "reason": reason} for stage, reason in _EXCLUDED_STAGES.items()
        ],
        "min_sample": _MIN_SOURCE_SAMPLE,
    }
