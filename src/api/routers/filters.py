from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api import ai, budget, db, events
from api.auth import AuthedUser, require_user
from api.models import FilterCreate, FilterPatch, ImprovePromptRequest
from core.filters import ON_AMBIGUOUS_VALUES, build_custom_instructions, compute_prompt_hash

router = APIRouter()

IMPROVE_MODEL = os.environ.get("JOBTRACKER_IMPROVE_MODEL", "gpt-5-mini")

_FILTER_COLS = "id, name, prompt, on_ambiguous, fail_closed, enabled, prompt_hash, created_at, updated_at"


def _hash(prompt: str, on_ambiguous: str) -> str:
    return compute_prompt_hash(build_custom_instructions(prompt, on_ambiguous))


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


@router.get("/user/filters")
def list_filters(user: AuthedUser = Depends(require_user)):
    return {
        "filters": db.query(
            f"SELECT {_FILTER_COLS} FROM user_filters WHERE user_id = %s ORDER BY id",
            (user.id,),
        )
    }


def _enqueue(user: AuthedUser, kind: str, payload: dict) -> tuple:
    """Returns (task_id, blocked_code). Never fails the enclosing save."""
    ent = budget.get_entitlement(user)
    if ent.key_source is None:
        return None, ("BUDGET_EXCEEDED" if ent.owner_key else "NO_API_KEY")
    row = db.query_one(
        "INSERT INTO tasks (kind, payload) VALUES (%s, %s) RETURNING id",
        (kind, db.jsonb(payload)),
    )
    assert row is not None
    events.publish_task(row["id"])
    return row["id"], None


@router.post("/user/filters")
def create_filter(body: FilterCreate, user: AuthedUser = Depends(require_user)):
    _validate_ambiguous(body.on_ambiguous)
    if db.query_one(
        "SELECT id FROM user_filters WHERE user_id = %s AND name = %s",
        (user.id, body.name),
    ):
        raise HTTPException(409, detail={"code": "DUPLICATE_NAME", "message": "filter name already exists"})
    row = db.query_one(
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
    task_id, blocked = (None, None)
    if body.enabled:
        task_id, blocked = _enqueue(
            user, "run_filter", {"user_id": user.id, "filter_id": row["id"]}
        )
    return {**row, "task_id": task_id, "run_blocked": blocked}


@router.patch("/user/filters/{filter_id}")
def patch_filter(filter_id: int, body: FilterPatch, user: AuthedUser = Depends(require_user)):
    existing = db.query_one(
        "SELECT * FROM user_filters WHERE id = %s AND user_id = %s", (filter_id, user.id)
    )
    if not existing:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, detail={"code": "EMPTY_PATCH", "message": "no fields to update"})
    if "on_ambiguous" in fields:
        _validate_ambiguous(fields["on_ambiguous"])
    prompt = fields.get("prompt", existing["prompt"])
    on_ambiguous = fields.get("on_ambiguous", existing["on_ambiguous"])
    fields["prompt_hash"] = _hash(prompt, on_ambiguous)
    cols = ", ".join(f"{k} = %({k})s" for k in fields)
    row = db.query_one(
        f"UPDATE user_filters SET {cols}, updated_at = now() "
        f"WHERE id = %(fid)s AND user_id = %(uid)s RETURNING {_FILTER_COLS}",
        {"fid": filter_id, "uid": user.id, **fields},
    )
    assert row is not None
    task_id, blocked = (None, None)
    hash_changed = row["prompt_hash"] != existing["prompt_hash"]
    if row["enabled"] and (hash_changed or fields.get("enabled")):
        task_id, blocked = _enqueue(
            user, "run_filter", {"user_id": user.id, "filter_id": filter_id}
        )
    return {**row, "task_id": task_id, "run_blocked": blocked}


@router.delete("/user/filters/{filter_id}")
def delete_filter(filter_id: int, user: AuthedUser = Depends(require_user)):
    db.execute(
        "DELETE FROM user_filters WHERE id = %s AND user_id = %s", (filter_id, user.id)
    )
    return {"ok": True}


@router.post("/user/filters/{filter_id}/run")
def run_filter(filter_id: int, user: AuthedUser = Depends(require_user)):
    if not db.query_one(
        "SELECT id FROM user_filters WHERE id = %s AND user_id = %s", (filter_id, user.id)
    ):
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown filter"})
    task_id, blocked = _enqueue(user, "run_filter", {"user_id": user.id, "filter_id": filter_id})
    if blocked:
        raise HTTPException(402, detail={"code": blocked, "message": "add your own API key or wait for the weekly budget to reset"})
    return {"task_id": task_id}


@router.post("/user/filters/run-all")
def run_all_filters(user: AuthedUser = Depends(require_user)):
    task_id, blocked = _enqueue(user, "run_all_filters", {"user_id": user.id})
    if blocked:
        raise HTTPException(402, detail={"code": blocked, "message": "add your own API key or wait for the weekly budget to reset"})
    return {"task_id": task_id}


@router.get("/filter-presets")
def list_presets(user: AuthedUser = Depends(require_user)):
    return {
        "presets": db.query(
            "SELECT id, name, description, prompt, on_ambiguous, fail_closed "
            "FROM filter_presets WHERE active ORDER BY name"
        )
    }


@router.post("/filter-presets/{preset_id}/adopt")
def adopt_preset(preset_id: int, user: AuthedUser = Depends(require_user)):
    preset = db.query_one(
        "SELECT * FROM filter_presets WHERE id = %s AND active", (preset_id,)
    )
    if not preset:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown preset"})
    name = preset["name"]
    if db.query_one(
        "SELECT id FROM user_filters WHERE user_id = %s AND name = %s", (user.id, name)
    ):
        raise HTTPException(
            409, detail={"code": "ALREADY_ADOPTED", "message": "this preset is already in your filters"}
        )
    row = db.query_one(
        f"""
        INSERT INTO user_filters (user_id, name, prompt, on_ambiguous, fail_closed, enabled, prompt_hash)
        VALUES (%s, %s, %s, %s, %s, TRUE, %s)
        RETURNING {_FILTER_COLS}
        """,
        (
            user.id,
            name,
            preset["prompt"],
            preset["on_ambiguous"],
            preset["fail_closed"],
            _hash(preset["prompt"], preset["on_ambiguous"]),
        ),
    )
    assert row is not None
    task_id, blocked = _enqueue(
        user, "run_filter", {"user_id": user.id, "filter_id": row["id"]}
    )
    return {**row, "task_id": task_id, "run_blocked": blocked}


class _ImprovedPrompt(BaseModel):
    improved: str
    rationale: str


@router.post("/ai/improve-prompt")
async def improve_prompt(body: ImprovePromptRequest, user: AuthedUser = Depends(require_user)):
    ent = budget.get_entitlement(user)
    try:
        cfg = budget.resolve_ai_config(user.id, ent)
    except PermissionError:
        raise HTTPException(402, detail={"code": "BUDGET_EXCEEDED", "message": "weekly budget exhausted"})
    except LookupError:
        raise HTTPException(402, detail={"code": "NO_API_KEY", "message": "add an API key to use AI features"})

    if cfg.key_source == "owner":
        cfg.model = IMPROVE_MODEL if IMPROVE_MODEL in ai.OWNER_KEY_MODELS else cfg.model
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
        _ImprovedPrompt,
        timeout=60.0,
    )
    if not parsed:
        raise HTTPException(502, detail={"code": "AI_ERROR", "message": "no response from model"})
    budget.record_usage(
        user.id,
        cfg.key_source,
        "improve_prompt",
        cfg.model,
        usage["prompt_tokens"],
        usage["completion_tokens"],
        usage["total_tokens"],
    )
    return {"improved": parsed.improved, "rationale": parsed.rationale}
