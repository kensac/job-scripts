"""What the market asks for, and where the user falls short of it.

The frequency table is the easy half and not the point: knowing that 41% of
targeted roles want Kubernetes is only useful next to whether the user has it.
Every endpoint here reads the same slice - the jobs this user can actually see -
so the market number and the gap number can never disagree about what "the roles
I'm targeting" means.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from api import criteria, db
from api.auth import AuthedUser, require_user
from api.routers.jobs import _VISIBILITY, _require_visible_job
from core import skills as skills_lib
from core.requirements import (
    CLEARANCE_LEVELS,
    DEGREE_LEVELS,
    EMPLOYMENT_TYPES,
    SENIORITIES,
    SKILL_KINDS,
)

router = APIRouter()


# How many skills a frequency table returns. Past this the tail is single-
# posting noise - one employer's in-house tool - which is not a market signal
# and pushes the signal off the screen.
TOP_SKILLS = 40


# The slice every endpoint here reads: the jobs this user can see, joined to
# what their pages said. _VISIBILITY is imported rather than restated - there
# are already three spellings of "can this user see this job" in the codebase
# and the whole point of the constant is that a fourth never gets written.
# job_requirements is url-keyed, so the join is on url, not on job id.
_VISIBLE = """
WITH visible AS (
{visibility}
)
"""

# Appended to _VISIBLE by the endpoints that need what a posting REQUIRES.
# The similarity route wants only the visible set, so it stops at _VISIBLE
# rather than dragging in a CTE it does not read.
_SLICE = """,
slice AS (
    SELECT DISTINCT r.* FROM visible v
    JOIN job_requirements r ON r.url = v.url
    WHERE r.has_requirements
      AND (%(seniority)s::text IS NULL OR r.seniority = %(seniority)s)
      AND (%(employment_type)s::text IS NULL OR r.employment_type = %(employment_type)s)
)
"""


def _visible_sql(body: str) -> str:
    return (
        _VISIBLE.format(
            visibility=_VISIBILITY.format(columns="j.url", criteria=criteria.SQL, extra="")
        )
        + body
    )


def _slice_sql(body: str) -> str:
    return _visible_sql(_SLICE + body)


def _params(user: AuthedUser, seniority: str | None, employment_type: str | None) -> dict[str, Any]:
    settings = db.query_one(
        "SELECT bypass_sponsorship_filter, criteria, background FROM user_settings "
        "WHERE user_id = %s",
        (user.id,),
    )
    return {
        "uid": user.id,
        "bypass_sponsorship": settings["bypass_sponsorship_filter"] if settings else True,
        "seniority": seniority,
        "employment_type": employment_type,
        **criteria.params(settings),
    }


def _background(user_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT background FROM user_settings WHERE user_id = %s", (user_id,))
    return (row or {}).get("background") or {}


@router.get("/requirements/market")
def market(
    user: AuthedUser = Depends(require_user),
    seniority: str | None = Query(default=None),
    employment_type: str | None = Query(default=None),
):
    """What the roles this user is targeting actually ask for.

    Counts are reported against `postings` - the number of postings in the
    slice - rather than as shares, because a share hides how thin a slice is.
    "Kubernetes in 3 of 7 postings" and "Kubernetes in 43% of postings" read the
    same and mean very different things.
    """
    params = _params(user, seniority, employment_type)
    total = db.query_one(_slice_sql("SELECT COUNT(*) AS postings FROM slice"), params)
    postings = (total or {}).get("postings", 0)
    if not postings:
        return {
            "postings": 0,
            "skills": {kind: [] for kind in SKILL_KINDS},
            "years_experience": {"stated": 0, "distribution": []},
            "degree": [],
            "clearance": [],
            "seniority": [],
            "employment_type": [],
            "flags": {},
        }

    skills = db.query(
        _slice_sql(
            """
            SELECT s.kind, s.skill, COUNT(DISTINCT s.url) AS postings
            FROM slice sl JOIN job_skills s ON s.url = sl.url
            GROUP BY s.kind, s.skill
            ORDER BY s.kind, postings DESC, s.skill
            """
        ),
        params,
    )
    by_kind: dict[str, list[dict]] = {kind: [] for kind in SKILL_KINDS}
    for row in skills:
        bucket = by_kind.setdefault(row["kind"], [])
        if len(bucket) < TOP_SKILLS:
            bucket.append({"skill": row["skill"], "postings": row["postings"]})

    yoe = db.query(
        _slice_sql(
            "SELECT yoe_min AS years, COUNT(*) AS postings FROM slice "
            "WHERE yoe_min IS NOT NULL GROUP BY yoe_min ORDER BY yoe_min"
        ),
        params,
    )
    degree = db.query(
        _slice_sql(
            """
            SELECT degree_min AS level, degree_required AS required, COUNT(*) AS postings
            FROM slice WHERE degree_min IS NOT NULL
            GROUP BY degree_min, degree_required
            ORDER BY array_position(%(degrees)s::text[], degree_min), required
            """
        ),
        {**params, "degrees": list(DEGREE_LEVELS)},
    )
    clearance = db.query(
        _slice_sql(
            "SELECT clearance AS level, COUNT(*) AS postings FROM slice "
            "WHERE clearance IS NOT NULL GROUP BY clearance "
            "ORDER BY array_position(%(clearances)s::text[], clearance)"
        ),
        {**params, "clearances": list(CLEARANCE_LEVELS)},
    )
    seniorities = db.query(
        _slice_sql(
            "SELECT seniority AS level, COUNT(*) AS postings FROM slice "
            "WHERE seniority IS NOT NULL GROUP BY seniority "
            "ORDER BY array_position(%(seniorities)s::text[], seniority)"
        ),
        {**params, "seniorities": list(SENIORITIES)},
    )
    employment = db.query(
        _slice_sql(
            "SELECT employment_type AS type, COUNT(*) AS postings FROM slice "
            "WHERE employment_type IS NOT NULL GROUP BY employment_type "
            "ORDER BY array_position(%(types)s::text[], employment_type)"
        ),
        {**params, "types": list(EMPLOYMENT_TYPES)},
    )
    # Deliberately reported beside `postings` rather than as percentages: these
    # are counts of postings that SAY something, and the silent majority is the
    # finding as often as the stated minority is.
    flags = db.query_one(
        _slice_sql(
            """
            SELECT COUNT(*) FILTER (WHERE yoe_min IS NOT NULL) AS states_years,
                   COUNT(*) FILTER (WHERE degree_min IS NOT NULL) AS states_degree,
                   COUNT(*) FILTER (WHERE degree_required) AS degree_required,
                   COUNT(*) FILTER (WHERE enrollment_required) AS enrollment_required,
                   COUNT(*) FILTER (WHERE clearance IS NOT NULL
                                      AND clearance != 'none') AS needs_clearance,
                   COUNT(*) FILTER (WHERE citizenship_required) AS citizenship_required,
                   COUNT(*) FILTER (WHERE sponsorship = 'offered') AS sponsorship_offered,
                   COUNT(*) FILTER (WHERE sponsorship = 'not_offered') AS sponsorship_refused
            FROM slice
            """
        ),
        params,
    )
    return {
        "postings": postings,
        "skills": by_kind,
        "years_experience": {
            "stated": sum(r["postings"] for r in yoe),
            "distribution": yoe,
        },
        "degree": degree,
        "clearance": clearance,
        "seniority": seniorities,
        "employment_type": employment,
        "flags": flags,
    }


@router.get("/requirements/gap")
def gap(
    user: AuthedUser = Depends(require_user),
    seniority: str | None = Query(default=None),
    employment_type: str | None = Query(default=None),
):
    """Where the user's stated background falls short of the slice.

    Each gap counts only postings that STATE the requirement. A posting silent
    about a degree is not evidence the user's degree is enough, and counting it
    as a pass would report a reachability number the market never promised.
    """
    background = _background(user.id)
    params = _params(user, seniority, employment_type)
    total = db.query_one(_slice_sql("SELECT COUNT(*) AS postings FROM slice"), params)
    postings = (total or {}).get("postings", 0)

    # Canonicalised on read, not on write: the user keeps their own spelling in
    # settings, and improving the alias table fixes every stored background at
    # once instead of only the ones saved since.
    have = {s for s in (skills_lib.canonical(x) for x in background.get("skills") or []) if s}

    missing: list[dict] = []
    strengths: list[dict] = []
    if postings:
        rows = db.query(
            _slice_sql(
                """
                SELECT s.skill, COUNT(DISTINCT s.url) AS postings
                FROM slice sl JOIN job_skills s ON s.url = sl.url
                WHERE s.kind = 'required'
                GROUP BY s.skill ORDER BY postings DESC, s.skill
                """
            ),
            params,
        )
        for row in rows:
            target = strengths if row["skill"] in have else missing
            if len(target) < TOP_SKILLS:
                target.append({"skill": row["skill"], "postings": row["postings"]})
        # A skill nobody in the slice asks for is worth saying out loud: it is
        # the half of the gap analysis that tells the user where their effort
        # is already spent rather than where to spend more.
        asked = {row["skill"] for row in rows}
        unused = sorted(have - asked)
    else:
        unused = sorted(have)

    yoe = background.get("yoe")
    degree = background.get("degree")
    clearance = background.get("clearance")
    citizen = background.get("citizen")
    needs_sponsorship = background.get("needs_sponsorship")
    blockers = db.query_one(
        _slice_sql(
            """
            SELECT
              COUNT(*) FILTER (WHERE %(yoe)s::int IS NOT NULL
                                 AND yoe_min IS NOT NULL
                                 AND yoe_min > %(yoe)s) AS years_short,
              COALESCE(MAX(yoe_min) FILTER (WHERE %(yoe)s::int IS NOT NULL
                                              AND yoe_min > %(yoe)s), 0) AS years_max_asked,
              COUNT(*) FILTER (WHERE %(degree)s::text IS NOT NULL
                                 AND degree_required AND degree_min IS NOT NULL
                                 AND array_position(%(degrees)s::text[], degree_min)
                                     > array_position(%(degrees)s::text[], %(degree)s)
                              ) AS degree_short,
              COUNT(*) FILTER (WHERE enrollment_required) AS enrollment_required,
              COUNT(*) FILTER (WHERE %(clearance)s::text IS NOT NULL
                                 AND clearance IS NOT NULL
                                 AND array_position(%(clearances)s::text[], clearance)
                                     > array_position(%(clearances)s::text[], %(clearance)s)
                              ) AS clearance_short,
              COUNT(*) FILTER (WHERE %(citizen)s::bool IS FALSE
                                 AND citizenship_required) AS citizenship_blocked,
              COUNT(*) FILTER (WHERE %(sponsor)s::bool IS TRUE
                                 AND sponsorship = 'not_offered') AS sponsorship_blocked
            FROM slice
            """
        ),
        {
            **params,
            "yoe": yoe,
            "degree": degree,
            "degrees": list(DEGREE_LEVELS),
            "clearance": clearance,
            "clearances": list(CLEARANCE_LEVELS),
            "citizen": citizen,
            "sponsor": needs_sponsorship,
        },
    )
    return {
        "postings": postings,
        "background_set": bool(background),
        "missing_skills": missing,
        "matching_skills": strengths,
        "unused_skills": unused,
        "blockers": blockers,
    }


# How many neighbours a similarity query returns. Small on purpose: the answer
# is a panel beside a posting, and the twentieth-most-similar role in a slice
# of a few thousand is not similar to anything.
SIMILAR_LIMIT = 10


@router.get("/jobs/{job_id}/similar")
def similar(job_id: int, user: AuthedUser = Depends(require_user)):
    """Postings that read like this one, among the ones this user can see.

    Gated through _require_visible_job rather than a fresh predicate: this is a
    per-job route, and per-job routes taking any of 49k ids is a bug this
    codebase has already shipped once. The neighbours are constrained to the
    same visible slice, so the route cannot become a way to read a posting the
    user could not otherwise see.

    No vector index backs this, deliberately. The slice is a fraction of the
    corpus, which makes an exact scan single-digit milliseconds and exact
    rather than approximate; the migration carries the measurements.
    """
    job = _require_visible_job(user, job_id, "j.id, j.url")
    anchor = db.query_one("SELECT embedding FROM job_embeddings WHERE url = %s", (job["url"],))
    if not anchor:
        # Not an error: the sweep is a backlog walker, so a posting ingested in
        # the last hour legitimately has no vector yet.
        raise HTTPException(
            404, detail={"code": "NOT_EMBEDDED", "message": "no embedding for this posting yet"}
        )
    params = _params(user, None, None)
    return {
        "job_id": job_id,
        "neighbours": db.query(
            _visible_sql(
                """
                SELECT j.id, j.company, j.title, j.url,
                       1 - (e.embedding <=> %(anchor)s::vector) AS similarity
                FROM visible v
                JOIN jobs j ON j.url = v.url
                JOIN job_embeddings e ON e.url = v.url
                WHERE v.url != %(url)s
                ORDER BY e.embedding <=> %(anchor)s::vector
                LIMIT %(limit)s
                """
            ),
            {
                **params,
                "anchor": str(anchor["embedding"]),
                "url": job["url"],
                "limit": SIMILAR_LIMIT,
            },
        ),
    }
