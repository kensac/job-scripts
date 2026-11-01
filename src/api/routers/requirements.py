"""What the market asks for, across the jobs one user can actually see.

The frequency table is scoped to the user's own visible slice rather than to
every posting, so "41% of the roles I'm targeting want Kubernetes" means the
roles they are targeting - the same set their board shows them.

A gap endpoint measured that table against a single stated background until
#301. It went with the background column: one profile is a ceiling on a person
who wants backend and frontend roles at once, and filters are already per
profile. It had never had data to run on and nothing consumed it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db, grouped
from api.auth import AuthedUser, require_user
from api.board import visibility
from api.board.access import require_visible_job
from api.problem import refuse
from core.derivations.requirements import (
    CLEARANCE_LEVELS,
    DEGREE_LEVELS,
    EMPLOYMENT_TYPES,
    SENIORITIES,
    SKILL_KINDS,
)

router = APIRouter()


class SkillCount(BaseModel):
    skill: str
    postings: int


class YearsCount(BaseModel):
    years: int
    postings: int


class YearsExperience(BaseModel):
    """`stated` is how many postings said anything at all. The silent
    majority is the finding as often as the stated minority is, which is why
    this is a count beside the distribution and not a percentage."""

    stated: int
    distribution: list[YearsCount]


class DegreeCount(BaseModel):
    level: str
    required: bool
    postings: int


class LevelCount(BaseModel):
    level: str
    postings: int


class TypeCount(BaseModel):
    type: str
    postings: int


class MarketFlags(BaseModel):
    """Counts of postings that SAY something, reported beside `postings`
    rather than as shares, for the same reason."""

    states_years: int
    states_degree: int
    degree_required: int
    enrollment_required: int
    needs_clearance: int
    citizenship_required: int
    sponsorship_offered: int
    sponsorship_refused: int


class Market(BaseModel):
    """What the roles this user is targeting ask for. Every count is against
    `postings`, the size of the slice, because a share hides how thin a slice
    is: "3 of 7" and "43%" read the same and mean very different things.

    `skills` is keyed by skill kind, which `core.derivations.requirements.SKILL_KINDS`
    defines, so an empty slice still answers with every kind present."""

    postings: int
    skills: dict[str, list[SkillCount]]
    years_experience: YearsExperience
    degree: list[DegreeCount]
    clearance: list[LevelCount]
    seniority: list[LevelCount]
    employment_type: list[TypeCount]
    flags: MarketFlags | None


class Neighbour(BaseModel):
    id: int
    company: str | None
    title: str | None
    url: str
    similarity: float


class Similar(BaseModel):
    job_id: int
    neighbours: list[Neighbour]


# How many skills a frequency table returns. Past this the tail is single-
# posting noise - one employer's in-house tool - which is not a market signal
# and pushes the signal off the screen.
TOP_SKILLS = 40


# The slice every endpoint here reads: the jobs this user can see, joined to
# what their pages said. The membership read is imported rather than restated - there
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
    return _VISIBLE.format(visibility=visibility.FAST.format(columns="j.url", extra="")) + body


def _slice_sql(body: str) -> str:
    return _visible_sql(_SLICE + body)


# The slice grouped by every key the page's cuts need, read once and folded
# by `market`. The flags are FILTERs inside each group, so a flag's rule stays
# in SQL and the fold only adds counts.
_MARKET_GROUPS = """
SELECT yoe_min, degree_min, degree_required AS required, clearance, seniority, employment_type,
       COUNT(*) AS postings,
       COUNT(*) FILTER (WHERE yoe_min IS NOT NULL) AS states_years,
       COUNT(*) FILTER (WHERE degree_min IS NOT NULL) AS states_degree,
       COUNT(*) FILTER (WHERE degree_required) AS degree_required,
       COUNT(*) FILTER (WHERE enrollment_required) AS enrollment_required,
       COUNT(*) FILTER (WHERE clearance IS NOT NULL AND clearance != 'none') AS needs_clearance,
       COUNT(*) FILTER (WHERE citizenship_required) AS citizenship_required,
       COUNT(*) FILTER (WHERE sponsorship = 'offered') AS sponsorship_offered,
       COUNT(*) FILTER (WHERE sponsorship = 'not_offered') AS sponsorship_refused
FROM slice
GROUP BY 1, 2, 3, 4, 5, 6
"""


def _tally(
    groups: list[dict[str, Any]], *keys: str, order: tuple[str, ...] | None
) -> list[tuple[tuple[Any, ...], int]]:
    """Postings per value of `keys`, leaving out rows whose first key is
    unstated. Sorted by the first key's place in `order` (unlisted last, as
    `array_position` sorts), or by its own value without one, then by the
    rest."""
    counts: dict[tuple[Any, ...], int] = {}
    for g in groups:
        key = tuple(g[k] for k in keys)
        if key[0] is not None:
            counts[key] = counts.get(key, 0) + g["postings"]

    def rank(key: tuple[Any, ...]) -> tuple[Any, ...]:
        if order is None:
            return key
        first = order.index(key[0]) if key[0] in order else len(order)
        return (first, *key[1:])

    return sorted(counts.items(), key=lambda item: rank(item[0]))


def _params(user: AuthedUser, seniority: str | None, employment_type: str | None) -> dict[str, Any]:
    return {
        "uid": user.id,
        "seniority": seniority,
        "employment_type": employment_type,
    }


@router.get("/requirements/market")
def market(
    user: AuthedUser = Depends(require_user),
    seniority: str | None = Query(default=None),
    employment_type: str | None = Query(default=None),
) -> Market:
    """What the roles this user is targeting actually ask for.

    Counts are reported against `postings` - the number of postings in the
    slice - rather than as shares, because a share hides how thin a slice is.
    "Kubernetes in 3 of 7 postings" and "Kubernetes in 43% of postings" read the
    same and mean very different things.
    """
    params = _params(user, seniority, employment_type)
    groups = db.query(_slice_sql(_MARKET_GROUPS), params)
    postings = sum(g["postings"] for g in groups)
    if not postings:
        return Market(
            postings=0,
            skills={kind: [] for kind in SKILL_KINDS},
            years_experience=YearsExperience(stated=0, distribution=[]),
            degree=[],
            clearance=[],
            seniority=[],
            employment_type=[],
            flags=None,
        )

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
    by_kind: dict[str, list[SkillCount]] = {kind: [] for kind in SKILL_KINDS}
    for row in skills:
        bucket = by_kind.setdefault(row["kind"], [])
        if len(bucket) < TOP_SKILLS:
            bucket.append(SkillCount(skill=row["skill"], postings=row["postings"]))

    years = [
        YearsCount(years=years, postings=n) for (years,), n in _tally(groups, "yoe_min", order=None)
    ]
    return Market(
        postings=postings,
        skills=by_kind,
        years_experience=YearsExperience(stated=sum(y.postings for y in years), distribution=years),
        degree=[
            DegreeCount(level=level, required=required, postings=n)
            for (level, required), n in _tally(
                groups, "degree_min", "required", order=DEGREE_LEVELS
            )
        ],
        clearance=[
            LevelCount(level=level, postings=n)
            for (level,), n in _tally(groups, "clearance", order=CLEARANCE_LEVELS)
        ],
        seniority=[
            LevelCount(level=level, postings=n)
            for (level,), n in _tally(groups, "seniority", order=SENIORITIES)
        ],
        employment_type=[
            TypeCount(type=kind, postings=n)
            for (kind,), n in _tally(groups, "employment_type", order=EMPLOYMENT_TYPES)
        ],
        flags=grouped.shaped(
            MarketFlags, grouped.fold(groups, (), counts=tuple(MarketFlags.model_fields))
        ),
    )


# How many neighbours a similarity query returns. Small on purpose: the answer
# is a panel beside a posting, and the twentieth-most-similar role in a slice
# of a few thousand is not similar to anything.
SIMILAR_LIMIT = 10


@router.get("/jobs/{job_id}/similar")
def similar(job_id: int, user: AuthedUser = Depends(require_user)) -> Similar:
    """Postings that read like this one, among the ones this user can see.

    Gated through require_visible_job rather than a fresh predicate: this is a
    per-job route, and per-job routes taking any of 49k ids is a bug this
    codebase has already shipped once. The neighbours are constrained to the
    same visible slice, so the route cannot become a way to read a posting the
    user could not otherwise see.

    No vector index backs this, deliberately. The slice is a fraction of the
    corpus, which makes an exact scan single-digit milliseconds and exact
    rather than approximate; the migration carries the measurements.
    """
    job = require_visible_job(user, job_id, "j.id, j.url")
    anchor = db.query_one("SELECT embedding FROM job_embeddings WHERE url = %s", (job["url"],))
    if not anchor:
        # Not an error: the sweep is a backlog walker, so a posting ingested in
        # the last hour legitimately has no vector yet.
        raise refuse(404, "NOT_EMBEDDED", "no embedding for this posting yet")
    params = _params(user, None, None)
    return Similar(
        job_id=job_id,
        neighbours=db.query_as(
            Neighbour,
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
    )
