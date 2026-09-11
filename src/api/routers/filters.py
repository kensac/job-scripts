from __future__ import annotations

import datetime
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from psycopg.errors import UniqueViolation
from pydantic import BaseModel

from api import ai, budget, db, filter_runs, task_admission
from api.ai import access as ai_access
from api.auth import AuthedUser, require_user
from api.board import visibility
from api.config import group_access_allowed
from api.models import FilterCreate, FilterPatch, ImprovePromptRequest, Ok
from api.problem import AI_REFUSALS
from api.routers.admin.shared import ADMIN_GROUPS
from core.filters import ON_AMBIGUOUS_VALUES, compute_filter_hash

router = APIRouter()

IMPROVE_MODEL = os.environ.get("JOBTRACKER_IMPROVE_MODEL", "gpt-5.6-luna")

_FILTER_COLS = (
    "id, name, prompt, on_ambiguous, fail_closed, enabled, prompt_hash, preset_id, "
    "created_at, updated_at"
)

_BLOCKED = budget.AccessReason | Literal["DEFERRED"]


class Filter(BaseModel):
    """`_FILTER_COLS`, in types. `preset_id` names the preset it was adopted
    from and is null for one written by hand; `prompt_hash` is what a verdict
    is keyed by, which is why two people on the same preset share verdicts."""

    id: int
    name: str
    prompt: str
    on_ambiguous: str
    fail_closed: bool
    enabled: bool
    prompt_hash: str
    preset_id: int | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class FilterRow(Filter):
    """A filter as the list shows it: the run in flight for this one, and
    whether another may start."""

    task: task_admission.InFlight | None
    run_admission: filter_runs.RunDecision


class FilterList(BaseModel):
    filters: list[FilterRow]
    run_all_task: task_admission.InFlight | None
    run_all_admission: filter_runs.RunDecision
    # Whether this person may START a run, which is a different question from
    # whether one may start NOW. `run_all_admission` answers the second: it
    # carries the budget reason, and the id of a run already in flight so the
    # page can show its progress. Folding permission into it would throw that
    # id away for exactly the people who cannot start one, who still have the
    # hourly sweep running on their behalf and still want to watch it.
    may_run_by_hand: bool
    may_run_message: str | None


class SavedFilter(Filter):
    """A filter as it stands after a write, with what the write set running.

    `run_blocked` is why nothing was queued: a budget reason, or DEFERRED
    when the hourly sweep will judge the board instead of this request.
    """

    task_id: int | None
    run_blocked: str | None
    run_blocked_message: str | None


class RunQueued(BaseModel):
    task_id: int | None


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


def _hash(prompt: str, on_ambiguous: str) -> str:
    return compute_filter_hash(prompt, on_ambiguous)


def _validate_ambiguous(value: str) -> str:
    if value not in ON_AMBIGUOUS_VALUES:
        raise HTTPException(
            400,
            detail={
                "code": "INVALID_ON_AMBIGUOUS",
                "message": f"on_ambiguous must be one of {ON_AMBIGUOUS_VALUES}",
            },
        )
    return value


def _refuse_second_enabled(user_id: int, except_id: int | None = None) -> None:
    """One enabled filter per person (Kanishk, 2026-09-08): a new account's
    three filters judged the whole catalog three times over, and one prompt
    can hold every condition. Enabling a second is refused with the name of
    the one that is on, so the person folds the two into one."""
    other = db.query_one(
        "SELECT id, name FROM user_filters WHERE user_id = %s AND enabled "
        "AND (%s::bigint IS NULL OR id <> %s) ORDER BY id LIMIT 1",
        (user_id, except_id, except_id),
    )
    if other:
        raise HTTPException(
            409,
            detail={
                "code": "ONE_FILTER",
                "message": (
                    f'One filter runs at a time, and "{other["name"]}" is on. '
                    "Turn it off first, or fold this into it: one prompt can hold "
                    "every condition you want."
                ),
                "enabled_filter_id": other["id"],
            },
        )


REJUDGE_GROUPS_KEY = "filter_rejudge_on_change_groups"
RUN_GROUPS_KEY = "filter_run_groups"
NOT_PERMITTED_MESSAGE = (
    "Starting a run by hand is limited to admins. Your board is still re-judged "
    "by the hourly sweep, at batch price, and fills in as verdicts land."
)
DEFERRED_MESSAGE = (
    "The board is re-judged under the new prompt at the next hourly sweep, at batch price, "
    "and fills back in as verdicts land. Press Run to re-evaluate now."
)


def _rejudge_on_change(user: AuthedUser) -> bool:
    return group_access_allowed(REJUDGE_GROUPS_KEY, user.groups)


def _may_run_by_hand(user: AuthedUser) -> bool:
    """Whether this person may start a run themselves.

    A run re-judges every posting in the catalog against a prompt. It is the
    most expensive thing a button in this product can do: three filter edits on
    one account cost 10.27 dollars in a day on 2026-09-08, which is what closed
    `filter_rejudge_on_change_groups`. That closed the automatic path and left
    the manual one open, so this closes the other half.

    Deliberately NOT folded into `filter_runs.admission`. That answers whether
    a run may start now, and carries the budget reason and the id of a run
    already in flight; overwriting it with a permission refusal would throw
    that id away for exactly the people who cannot start one, who still have
    the hourly sweep running for them and still want to watch it.
    """
    return bool(ADMIN_GROUPS.intersection(user.groups)) or group_access_allowed(
        RUN_GROUPS_KEY, user.groups
    )


def _refuse_unpermitted_run(user: AuthedUser) -> None:
    if not _may_run_by_hand(user):
        raise HTTPException(403, detail={"code": "NOT_PERMITTED", "message": NOT_PERMITTED_MESSAGE})


def _enqueue_on_change(user: AuthedUser, filter_id: int) -> tuple[int | None, _BLOCKED | None]:
    """A save that would run the filter: runs it when the person's group
    re-judges on change, otherwise says the sweep will (Kanishk, 2026-09-09:
    stop recomputing whenever a filter changes, behind a flag)."""
    if not _rejudge_on_change(user):
        return None, "DEFERRED"
    return _enqueue(user, filter_id, defer_conflict=True)


def _blocked_message(user: AuthedUser, blocked: _BLOCKED | None) -> str | None:
    if blocked == "DEFERRED":
        return DEFERRED_MESSAGE
    return budget.access_message(blocked, budget.get_entitlement(user)) if blocked else None


def _running(
    user_id: int, kind: str, filter_id: int | None = None
) -> task_admission.InFlight | None:
    """The run of this kind still in flight for this person (for one filter
    when given), if any. A parent that split into chunks is 'waiting', not
    'running', and is still in flight.

    `kind` is selected because the shape a task in flight has is declared
    once, beside `task_admission.in_flight`, rather than copied per query."""
    return db.query_one_as(
        task_admission.InFlight,
        """
        SELECT id, kind, status, progress, created_at FROM tasks
        WHERE kind = %(kind)s
          AND status IN ('pending', 'running', 'awaiting_batch', 'waiting')
          AND (payload->>'user_id')::bigint = %(uid)s
          AND (%(fid)s::bigint IS NULL OR (payload->>'filter_id')::bigint = %(fid)s)
        ORDER BY id DESC LIMIT 1
        """,
        {"kind": kind, "uid": user_id, "fid": filter_id},
    )


def _refuse_second_run(running: task_admission.InFlight | None) -> None:
    """A second run while the first is in flight is refused, not queued: the
    page disabled its button only while it remembered its own task id, so a
    reload could queue the same run twice while the first parked on the
    provider's batch."""
    if running:
        raise HTTPException(
            409,
            detail={
                "code": "IN_PROGRESS",
                "message": "this run is already in progress",
                "task_id": running.id,
            },
        )


@router.get("/user/filters")
def list_filters(user: AuthedUser = Depends(require_user)) -> FilterList:
    """Each filter carries its in-flight run, and the list carries the
    in-flight run-all, so a button is disabled from server state."""
    rows = db.query_as(
        Filter,
        f"SELECT {_FILTER_COLS} FROM user_filters WHERE user_id = %s ORDER BY id",
        (user.id,),
    )
    access_failure = budget.access_failure(user)
    may_run = _may_run_by_hand(user)
    return FilterList(
        filters=[
            FilterRow(
                **row.model_dump(),
                task=_running(user.id, "run_filter", row.id),
                run_admission=filter_runs.admission(
                    user.id, row.id, access_failure=access_failure
                ).decision(),
            )
            for row in rows
        ],
        run_all_task=_running(user.id, "run_all_filters"),
        run_all_admission=filter_runs.admission(
            user.id, None, access_failure=access_failure
        ).decision(),
        may_run_by_hand=may_run,
        may_run_message=None if may_run else NOT_PERMITTED_MESSAGE,
    )


def _enqueue(
    user: AuthedUser, filter_id: int | None, *, defer_conflict: bool = False
) -> tuple[int | None, _BLOCKED | None]:
    decision = filter_runs.admission(user.id, filter_id, access_failure=budget.access_failure(user))
    if decision.access_failure:
        return None, decision.access_failure.reason
    result = (
        decision
        if decision.conflict
        else filter_runs.enqueue(user.id, filter_id, policy="interactive")
    )
    if result.conflict:
        if defer_conflict:
            return None, "DEFERRED"
        _refuse_second_run(result.conflict)
    return result.task_id, None


def _after_filter_change(user: AuthedUser, row: Filter, previous: Filter | None) -> SavedFilter:
    needs_judgement = row.enabled and (
        previous is None or not previous.enabled or row.prompt_hash != previous.prompt_hash
    )
    task_id, blocked = _enqueue_on_change(user, row.id) if needs_judgement else (None, None)
    visibility.request_refresh(user.id)
    return SavedFilter(
        **row.model_dump(),
        task_id=task_id,
        run_blocked=blocked,
        run_blocked_message=_blocked_message(user, blocked),
    )


@router.post("/user/filters")
def create_filter(body: FilterCreate, user: AuthedUser = Depends(require_user)) -> SavedFilter:
    with db.transaction():
        db.query_one("SELECT id FROM users WHERE id = %s FOR UPDATE", (user.id,))
        _validate_ambiguous(body.on_ambiguous)
        if db.query_one(
            "SELECT id FROM user_filters WHERE user_id = %s AND name = %s",
            (user.id, body.name),
        ):
            raise HTTPException(
                409, detail={"code": "DUPLICATE_NAME", "message": "filter name already exists"}
            )
        if body.enabled:
            _refuse_second_enabled(user.id)
        row = db.query_one_as(
            Filter,
            f"""
            INSERT INTO user_filters (user_id, name, prompt, on_ambiguous, fail_closed, enabled, prompt_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING {_FILTER_COLS}
            """,
            (
                user.id,
                body.name,
                body.prompt,
                body.on_ambiguous,
                body.fail_closed,
                body.enabled,
                _hash(body.prompt, body.on_ambiguous),
            ),
        )
        assert row is not None
    return _after_filter_change(user, row, None)


@router.patch("/user/filters/{filter_id}")
def patch_filter(
    filter_id: int, body: FilterPatch, user: AuthedUser = Depends(require_user)
) -> SavedFilter:
    with db.transaction():
        db.query_one("SELECT id FROM users WHERE id = %s FOR UPDATE", (user.id,))
        # The columns, not a star: this row is compared against the one the
        # update returns, and a star and the shape reading it drift in silence.
        existing = db.query_one_as(
            Filter,
            f"SELECT {_FILTER_COLS} FROM user_filters WHERE id = %s AND user_id = %s",
            (filter_id, user.id),
        )
        if not existing:
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise HTTPException(
                400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"}
            )
        if "on_ambiguous" in fields:
            _validate_ambiguous(fields["on_ambiguous"])
        if fields.get("enabled") and not existing.enabled:
            _refuse_second_enabled(user.id, filter_id)
        prompt = fields.get("prompt", existing.prompt)
        on_ambiguous = fields.get("on_ambiguous", existing.on_ambiguous)
        fields["prompt_hash"] = _hash(prompt, on_ambiguous)
        cols = ", ".join(f"{k} = %({k})s" for k in fields)
        try:
            row = db.query_one_as(
                Filter,
                f"UPDATE user_filters SET {cols}, updated_at = now() "
                f"WHERE id = %(fid)s AND user_id = %(uid)s RETURNING {_FILTER_COLS}",
                {"fid": filter_id, "uid": user.id, **fields},
            )
        except UniqueViolation as exc:
            # create_filter pre-checks the name; renaming has to answer the same
            # way rather than letting user_filters_user_id_name_key escape as a 500.
            raise HTTPException(
                409, detail={"code": "DUPLICATE_NAME", "message": "filter name already exists"}
            ) from exc
        assert row is not None
    return _after_filter_change(user, row, existing)


@router.delete("/user/filters/{filter_id}")
def delete_filter(filter_id: int, user: AuthedUser = Depends(require_user)) -> Ok:
    db.execute("DELETE FROM user_filters WHERE id = %s AND user_id = %s", (filter_id, user.id))
    visibility.request_refresh(user.id)
    return Ok()


@router.post("/user/filters/{filter_id}/run")
def run_filter(filter_id: int, user: AuthedUser = Depends(require_user)) -> RunQueued:
    _refuse_unpermitted_run(user)
    if not db.query_one(
        "SELECT id FROM user_filters WHERE id = %s AND user_id = %s", (filter_id, user.id)
    ):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
    task_id, blocked = _enqueue(user, filter_id)
    if blocked:
        raise HTTPException(
            402, detail={"code": blocked, "message": _blocked_message(user, blocked)}
        )
    return RunQueued(task_id=task_id)


@router.post("/user/filters/run-all")
def run_all_filters(user: AuthedUser = Depends(require_user)) -> RunQueued:
    _refuse_unpermitted_run(user)
    task_id, blocked = _enqueue(user, None)
    if blocked:
        raise HTTPException(
            402, detail={"code": blocked, "message": _blocked_message(user, blocked)}
        )
    return RunQueued(task_id=task_id)


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
_PRESET_COVERAGE_SQL = """
WITH q AS (
    SELECT j.id AS job_id, j.url AS url, a.check_type, a.status, a.id AS qid
    FROM ai_queries a
    JOIN jobs j ON j.url = a.url
    WHERE j.active AND a.check_type IN ('closed', 'clearance')
      AND a.status IN ('passed', 'rejected')
), latest AS (
    -- Deduping on jobs.id rather than the url text is load-bearing for speed,
    -- not tidiness: the url-keyed version of this pair of queries measured
    -- 134s and 20s against production. Sorting integers keeps it in memory.
    SELECT DISTINCT ON (job_id, check_type) job_id, url, check_type, status
    FROM q ORDER BY job_id, check_type, qid DESC
), eligible AS (
    -- Both gates passed. HAVING count(*) = 2 beats a self-join on the same
    -- CTE, which is what made the original slow.
    SELECT min(url) AS url
    FROM latest WHERE status = 'passed'
    GROUP BY job_id HAVING count(*) = 2
), judged AS (
    SELECT DISTINCT ON (url, prompt_hash) url, prompt_hash, status
    FROM ai_queries
    WHERE check_type = 'custom' AND prompt_hash = ANY(%(hashes)s)
      AND status IN ('passed', 'rejected')
    ORDER BY url, prompt_hash, id DESC
)
SELECT jd.prompt_hash,
       count(*) AS judged,
       count(*) FILTER (WHERE jd.status = 'passed') AS would_show,
       (SELECT count(*) FROM eligible) AS eligible
FROM eligible e JOIN judged jd ON jd.url = e.url
GROUP BY jd.prompt_hash
"""

_ELIGIBLE_SQL = """
WITH q AS (
    SELECT j.id AS job_id, a.check_type, a.status, a.id AS qid
    FROM ai_queries a
    JOIN jobs j ON j.url = a.url
    WHERE j.active AND a.check_type IN ('closed', 'clearance')
      AND a.status IN ('passed', 'rejected')
), latest AS (
    SELECT DISTINCT ON (job_id, check_type) job_id, check_type, status
    FROM q ORDER BY job_id, check_type, qid DESC
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
    hashes = {p.id: _hash(p.prompt, p.on_ambiguous) for p in presets}
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
                coverage=_coverage(coverage.get(hashes[preset.id]), eligible),
            )
            for preset in presets
        ],
        eligible_postings=eligible,
    )


def _coverage(row: dict | None, eligible: int) -> PresetCoverage:
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
            raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown preset"})
        name = preset.name
        # Adopted is a fact on the row (preset_id), not a guess from the name: a
        # renamed adopted filter is still adopted, and a hand-written filter that
        # happens to share the name still blocks, because the name is unique.
        if db.query_one(
            "SELECT id FROM user_filters WHERE user_id = %s AND (preset_id = %s OR name = %s)",
            (user.id, preset_id, name),
        ):
            raise HTTPException(
                409,
                detail={
                    "code": "ALREADY_ADOPTED",
                    "message": "this preset is already in your filters",
                },
            )
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
                _hash(preset.prompt, preset.on_ambiguous),
                preset_id,
            ),
        )
        assert row is not None
    return _after_filter_change(user, row, None)


class ImprovedPrompt(BaseModel):
    """What the model wrote, and what it says it changed. The same shape it
    is asked for and the shape this route returns, because there is nothing
    in between."""

    improved: str
    rationale: str


@router.post("/ai/improve-prompt", responses=AI_REFUSALS)
async def improve_prompt(
    body: ImprovePromptRequest, user: AuthedUser = Depends(require_user)
) -> ImprovedPrompt:
    cfg = ai_access.require_config(user)

    if cfg.key_source == "owner":
        cfg.model = IMPROVE_MODEL if IMPROVE_MODEL in ai.OWNER_KEY_MODELS else cfg.model
    with budget.record_parse_failures(user.id, cfg.key_source, "improve_prompt", cfg.model):
        parsed, usage = await ai.parse(
            cfg,
            (
                "You rewrite rough job-filter prompts into the structure this system's "
                "best-performing filters use. A later AI reads one job posting plus the "
                "rewritten prompt and decides keep-or-filter, so the prompt must make that "
                "decision fast and unambiguous - decisive prompts also cost fewer reasoning "
                "tokens.\n\n"
                "Rewrite into this proven shape:\n"
                "1. One opening line stating who/what the filter is for, if inferable.\n"
                "2. A 'KEEP' section: concrete inclusions - role titles, domains, skills, "
                "company types. Name examples rather than describing vibes.\n"
                "3. A 'FILTER OUT' section: concrete exclusions, phrased either 'only when "
                "clearly ...' (lenient) or 'if ANY of these apply (each is sufficient on its "
                "own)' (strict) - pick whichever matches the user's evident intent.\n"
                "4. Numbers over adjectives: turn 'well paid' into a threshold with an explicit "
                "reading rule (e.g. 'judge on the TOP of a stated range'); turn 'junior' into "
                "an experience/degree rule with required-vs-preferred distinguished.\n"
                "5. End with one explicit ambiguity rule: 'When uncertain, KEEP.' or 'If you "
                "cannot confirm the criteria, FILTER OUT.' - matching the user's intent "
                "(default to KEEP when unclear which they want).\n\n"
                "Keep the user's intent exactly; do not invent criteria they did not imply. "
                "Do not add instructions about output format or reasons - the system appends "
                "those. rationale: <=50 words on what you changed and why."
            ),
            body.prompt,
            ImprovedPrompt,
            timeout=60.0,
        )
    budget.record_tokens(
        user.id,
        cfg.key_source,
        "improve_prompt",
        cfg.model,
        usage,
    )
    if not parsed:
        raise HTTPException(502, detail={"code": "AI_ERROR", "message": "no response from model"})
    return parsed
