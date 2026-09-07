"""Experiments: measure one AI step across models and efforts on a sample,
through the production path. See api.tasks.experiments."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db, events
from api.auth import AuthedUser
from api.routers.admin import require_admin
from api.tasks import experiments as exp

router = APIRouter(prefix="/admin")

_COLS = "id, purpose, params, status, created_by, task_id, summary, error, created_at, finished_at"


class Arm(BaseModel):
    model: str = Field(min_length=1, max_length=100)
    effort: str = Field(min_length=1, max_length=20)


class ExperimentCreate(BaseModel):
    purpose: str
    arms: list[Arm] = Field(min_length=1, max_length=16)
    sample: int = Field(default=100, ge=1, le=2000)
    # Any string; the same seed draws the same postings on a later run.
    seed: str | None = Field(default=None, max_length=100)
    filter_id: int | None = None
    # The arm every other arm is scored against; default: the dearest arm.
    reference: str | None = None


@router.get("/experiments")
def list_experiments(user: AuthedUser = Depends(require_admin)):
    """The runs, plus what a form needs to make a new one: the steps, every
    chat model with the efforts it accepts (the runner refuses the rest), and
    every filter an experiment on the filter step can name."""
    from core import providers

    models = [
        {"model": name, "provider": provider, "efforts": list(declared.reasoning.accepts)}
        for name, (provider, declared) in sorted(providers.MODELS.items())
        if declared.reasoning.accepts
    ]
    return {
        "steps": sorted(exp.steps()),
        "models": models,
        "filters": db.query(
            "SELECT f.id, f.name, f.user_id, u.email AS user_email, f.enabled "
            "FROM user_filters f JOIN users u ON u.id = f.user_id ORDER BY f.user_id, f.id"
        ),
        "experiments": db.query(f"SELECT {_COLS} FROM ai_experiments ORDER BY id DESC LIMIT 50"),
    }


@router.post("/experiments", status_code=202)
def create_experiment(body: ExperimentCreate, user: AuthedUser = Depends(require_admin)):
    if body.purpose not in exp.steps():
        raise HTTPException(
            400,
            detail={"code": "UNKNOWN_STEP", "message": f"measurable steps: {sorted(exp.steps())}"},
        )
    if body.purpose == "filter" and not body.filter_id:
        raise HTTPException(
            400,
            detail={
                "code": "FILTER_REQUIRED",
                "message": "an experiment on filter needs filter_id",
            },
        )
    refused = {
        exp.arm_name(a.model, a.effort): why
        for a in body.arms
        if (why := exp.arm_ok(a.model, a.effort))
    }
    if len(refused) == len(body.arms):
        raise HTTPException(
            400, detail={"code": "NO_ARM", "message": "no arm can run", "arms": refused}
        )
    names = {exp.arm_name(a.model, a.effort) for a in body.arms}
    if body.reference and body.reference not in names:
        raise HTTPException(
            400,
            detail={"code": "BAD_REFERENCE", "message": "reference must name one of the arms"},
        )
    params: dict[str, Any] = {
        "sample": body.sample,
        "seed": body.seed,
        "arms": [a.model_dump() for a in body.arms],
        "filter_id": body.filter_id,
        "reference": body.reference,
    }
    row = db.query_one(
        f"INSERT INTO ai_experiments (purpose, params, created_by) VALUES (%s, %s, %s) RETURNING {_COLS}",
        (body.purpose, db.jsonb(params), user.id),
    )
    assert row is not None
    task = db.query_one(
        "INSERT INTO tasks (kind, payload) VALUES ('run_experiment', %s) RETURNING id",
        (db.jsonb({"experiment_id": row["id"], "user_id": user.id}),),
    )
    assert task is not None
    db.execute("UPDATE ai_experiments SET task_id = %s WHERE id = %s", (task["id"], row["id"]))
    events.publish_task(task["id"])
    return {**row, "task_id": task["id"], "refused_arms": refused}


@router.get("/experiments/{experiment_id}")
def get_experiment(
    experiment_id: int, arm: str | None = None, user: AuthedUser = Depends(require_admin)
):
    row = db.query_one(f"SELECT {_COLS} FROM ai_experiments WHERE id = %s", (experiment_id,))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown experiment"})
    results = db.query(
        "SELECT arm, url, output, usage, cost_usd, error FROM ai_experiment_results "
        "WHERE experiment_id = %s" + (" AND arm = %s" if arm else "") + " ORDER BY url, arm",
        (experiment_id, arm) if arm else (experiment_id,),
    )
    return {**row, "results": results}
