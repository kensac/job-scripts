"""Shared filter presets and their current verdict coverage."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser, require_user
from api.problem import refuse
from api.routers.filtering.saved_filters import (
    _FILTER_COLS,
    Filter,
    SavedFilter,
    _after_filter_change,
    _filter_prompt_hash,
    _refuse_second_enabled,
)
from core import catalog, verdict_reads

router = APIRouter()


class PresetCoverage(BaseModel):
    """Counts, not a rate. "170 of 7,397 already judged" is the sentence; a
    percentage hides that the remainder is unjudged rather than rejected."""

    would_show_now: int
    already_judged: int
    eligible: int
    needs_ai: int


class FilterPreset(BaseModel):
    id: int
    name: str
    description: str | None
    prompt: str
    on_ambiguous: str
    fail_closed: bool


class PresetOffer(FilterPreset):
    """A preset with what it would show today, so adopting one is not blind."""

    coverage: PresetCoverage


class PresetList(BaseModel):
    presets: list[PresetOffer]
    eligible_postings: int


# What a preset would show TODAY, without spending anything.
#
# Custom verdicts key on (url, check_type, prompt_hash) and prompt_hash is
# computed from the prompt text and on_ambiguous alone - nothing user-specific
# - so two people running the same preset share a cache. Verified against
# production: all 10 stored user_filters hashes equal hash(prompt,
# on_ambiguous) exactly.
#
# That matters most for someone with no API key, who would otherwise face
# 7,397 postings that pass the closed and clearance gates with no way to
# narrow them. Adopting a preset with coverage turns that into a readable
# board immediately. Adopting one WITHOUT coverage shows an empty board until
# AI runs, which is worse than the wall - so the numbers ship with the preset
# rather than the choice being made blind. Today one preset of eleven has any
# cached verdicts at all.
# Deduping on jobs.id rather than the url text is load-bearing for speed,
# not tidiness: the url-keyed version of this pair of queries measured 134s
# and 20s against production. Sorting integers keeps it in memory.
_LATEST_GATES = verdict_reads.latest_per(
    "j.id, v.check_type",
    "j.id AS job_id, j.url AS url, v.check_type, v.status",
    catalog.IS_AVAILABLE.format(job="j") + " AND v.check_type IN ('closed', 'clearance')",
    join="JOIN jobs j ON j.url = v.url",
)

_LATEST_JUDGED = verdict_reads.latest_per(
    "url, prompt_hash",
    "url, prompt_hash, status",
    "check_type = 'custom' AND prompt_hash = ANY(%(hashes)s)",
)

_PRESET_COVERAGE_SQL = f"""
WITH latest AS (
    {_LATEST_GATES}
), eligible AS (
    -- Both gates passed. HAVING count(*) = 2 beats a self-join on the same
    -- CTE, which is what made the original slow.
    SELECT min(url) AS url
    FROM latest WHERE status = 'passed'
    GROUP BY job_id HAVING count(*) = 2
), judged AS (
    {_LATEST_JUDGED}
)
SELECT jd.prompt_hash,
       count(*) AS judged,
       count(*) FILTER (WHERE jd.status = 'passed') AS would_show,
       (SELECT count(*) FROM eligible) AS eligible
FROM eligible e JOIN judged jd ON jd.url = e.url
GROUP BY jd.prompt_hash
"""

_ELIGIBLE_SQL = f"""
WITH latest AS (
    {_LATEST_GATES}
)
SELECT count(*) AS eligible FROM (
    SELECT job_id FROM latest WHERE status = 'passed'
    GROUP BY job_id HAVING count(*) = 2
) both_gates
"""


@router.get("/filter-presets")
def list_presets(user: AuthedUser = Depends(require_user)) -> PresetList:
    presets = db.query_as(
        FilterPreset,
        "SELECT id, name, description, prompt, on_ambiguous, fail_closed "
        "FROM filter_presets WHERE active ORDER BY name",
    )
    hashes = {p.id: _filter_prompt_hash(p.prompt, p.on_ambiguous) for p in presets}
    coverage = {
        row["prompt_hash"]: row
        for row in db.query(_PRESET_COVERAGE_SQL, {"hashes": list(hashes.values())})
    }
    eligible_row = db.query_one(_ELIGIBLE_SQL)
    eligible = int(eligible_row["eligible"]) if eligible_row else 0
    return PresetList(
        presets=[
            PresetOffer(
                **preset.model_dump(),
                coverage=_preset_coverage(coverage.get(hashes[preset.id]), eligible),
            )
            for preset in presets
        ],
        eligible_postings=eligible,
    )


def _preset_coverage(row: dict | None, eligible: int) -> PresetCoverage:
    judged = int(row["judged"]) if row else 0
    return PresetCoverage(
        would_show_now=int(row["would_show"]) if row else 0,
        already_judged=judged,
        eligible=eligible,
        # Postings this preset has never been run against. They are not
        # rejections - nothing has looked at them, and looking costs an AI
        # call the adopter may not be able to make.
        needs_ai=max(eligible - judged, 0),
    )


@router.post("/filter-presets/{preset_id}/adopt")
def adopt_preset(preset_id: int, user: AuthedUser = Depends(require_user)) -> SavedFilter:
    with db.transaction():
        db.query_one("SELECT id FROM users WHERE id = %s FOR UPDATE", (user.id,))
        # Named columns rather than a star, for the same reason as the patch
        # above: the row and what reads it can then only drift together.
        preset = db.query_one_as(
            FilterPreset,
            "SELECT id, name, description, prompt, on_ambiguous, fail_closed "
            "FROM filter_presets WHERE id = %s AND active",
            (preset_id,),
        )
        if not preset:
            raise refuse(404, "NOT_FOUND", "unknown preset")
        name = preset.name
        # Adopted is a fact on the row (preset_id), not a guess from the name: a
        # renamed adopted filter is still adopted, and a hand-written filter that
        # happens to share the name still blocks, because the name is unique.
        if db.query_one(
            "SELECT id FROM user_filters WHERE user_id = %s AND (preset_id = %s OR name = %s)",
            (user.id, preset_id, name),
        ):
            raise refuse(409, "ALREADY_ADOPTED", "this preset is already in your filters")
        _refuse_second_enabled(user.id)
        row = db.query_one_as(
            Filter,
            f"""
            INSERT INTO user_filters
                (user_id, name, prompt, on_ambiguous, fail_closed, enabled, prompt_hash, preset_id)
            VALUES (%s, %s, %s, %s, %s, TRUE, %s, %s)
            RETURNING {_FILTER_COLS}
            """,
            (
                user.id,
                name,
                preset.prompt,
                preset.on_ambiguous,
                preset.fail_closed,
                _filter_prompt_hash(preset.prompt, preset.on_ambiguous),
                preset_id,
            ),
        )
        assert row is not None
    return _after_filter_change(user, row, None)
