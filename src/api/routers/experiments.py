"""Experiments: measure one AI step across models and efforts on a sample,
through the production path."""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db, events
from api import experiments as exp
from api.auth import AuthedUser
from api.routers.admin import require_admin

router = APIRouter(prefix="/admin")

_COLS = "id, purpose, params, status, created_by, task_id, summary, error, created_at, finished_at"


class Experiment(BaseModel):
    """`_COLS`, in types. `params` is the request as it was made (arms,
    sample, seed) and `summary` is what scoring made of the answers; both are
    shaped by the step being measured, which is why neither is declared
    further."""

    id: int
    purpose: str
    params: dict[str, Any]
    status: str
    created_by: int | None
    task_id: int | None
    summary: dict[str, Any] | None
    error: str | None
    created_at: datetime.datetime
    finished_at: datetime.datetime | None


class MeasurableModel(BaseModel):
    """A chat model an experiment may name, with the efforts it accepts. The
    runner refuses the rest, so the form only offers these."""

    model: str
    provider: str
    efforts: list[str]


class NameableFilter(BaseModel):
    id: int
    name: str
    user_id: int
    user_email: str
    enabled: bool


class ExperimentIndex(BaseModel):
    """The runs, plus what a form needs to make a new one."""

    steps: list[str]
    models: list[MeasurableModel]
    filters: list[NameableFilter]
    experiments: list[Experiment]


class ExperimentQueued(Experiment):
    """The run as stored, plus the task that will carry it out and the arms
    that were asked for and cannot run. An arm is refused for a reason the
    caller can act on, so it is returned rather than dropped."""

    refused_arms: dict[str, str]


class Rescored(BaseModel):
    id: int
    summary: dict[str, Any]


class ArmResult(BaseModel):
    """One arm's answer for one posting. `output` is shaped by the step being
    measured."""

    arm: str
    url: str
    output: dict[str, Any] | None
    usage: dict[str, Any] | None
    # float, not Decimal: pydantic serialises a Decimal as a string, and this
    # shipped declaring one, so the cost came back quoted where it used to be a
    # number.
    cost_usd: float | None
    error: str | None


class ExperimentDetail(Experiment):
    results: list[ArmResult]


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
def list_experiments(user: AuthedUser = Depends(require_admin)) -> ExperimentIndex:
    """The runs, plus what a form needs to make a new one: the steps, every
    chat model with the efforts it accepts (the runner refuses the rest), and
    every filter an experiment on the filter step can name."""
    from core import providers

    return ExperimentIndex(
        steps=sorted(exp.steps()),
        models=[
            MeasurableModel(model=name, provider=provider, efforts=list(d.reasoning.accepts))
            for name, (provider, d) in sorted(providers.MODELS.items())
            if d.reasoning.accepts
        ],
        filters=db.query_as(
            NameableFilter,
            "SELECT f.id, f.name, f.user_id, u.email AS user_email, f.enabled "
            "FROM user_filters f JOIN users u ON u.id = f.user_id ORDER BY f.user_id, f.id",
        ),
        experiments=db.query_as(
            Experiment, f"SELECT {_COLS} FROM ai_experiments ORDER BY id DESC LIMIT 50"
        ),
    )


@router.post("/experiments", status_code=202)
def create_experiment(
    body: ExperimentCreate, user: AuthedUser = Depends(require_admin)
) -> ExperimentQueued:
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
    row = db.query_one_as(
        Experiment,
        f"INSERT INTO ai_experiments (purpose, params, created_by) VALUES (%s, %s, %s) RETURNING {_COLS}",
        (body.purpose, db.jsonb(params), user.id),
    )
    assert row is not None
    task = db.query_one(
        "INSERT INTO tasks (kind, payload) VALUES ('run_experiment', %s) RETURNING id",
        (db.jsonb({"experiment_id": row.id, "user_id": user.id}),),
    )
    assert task is not None
    db.execute("UPDATE ai_experiments SET task_id = %s WHERE id = %s", (task["id"], row.id))
    events.publish_task(task["id"])
    return ExperimentQueued(**{**row.model_dump(), "task_id": task["id"]}, refused_arms=refused)


@router.post("/experiments/{experiment_id}/rescore")
def rescore_experiment(experiment_id: int, user: AuthedUser = Depends(require_admin)) -> Rescored:
    """Score the stored answers again. The answers are the paid-for part;
    the scoring is code, and code changes. The first requirements run
    collected all 800 answers and then failed on a column name in the
    scoring query, and nothing should have to be re-bought to fix that."""
    row = db.query_one("SELECT id FROM ai_experiments WHERE id = %s", (experiment_id,))
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown experiment"})
    if not db.query_one(
        "SELECT 1 FROM ai_experiment_results WHERE experiment_id = %s LIMIT 1", (experiment_id,)
    ):
        raise HTTPException(
            409, detail={"code": "NO_RESULTS", "message": "nothing collected yet to score"}
        )
    summary = exp.summarise(experiment_id)
    db.execute(
        "UPDATE ai_experiments SET summary = %s, status = 'done', error = NULL, "
        "finished_at = COALESCE(finished_at, now()) WHERE id = %s",
        (db.jsonb(summary), experiment_id),
    )
    return Rescored(id=experiment_id, summary=summary)


@router.get("/experiments/{experiment_id}")
def get_experiment(
    experiment_id: int, arm: str | None = None, user: AuthedUser = Depends(require_admin)
) -> ExperimentDetail:
    row = db.query_one_as(
        Experiment, f"SELECT {_COLS} FROM ai_experiments WHERE id = %s", (experiment_id,)
    )
    if not row:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "unknown experiment"})
    results = db.query_as(
        ArmResult,
        "SELECT arm, url, output, usage, cost_usd, error FROM ai_experiment_results "
        "WHERE experiment_id = %s" + (" AND arm = %s" if arm else "") + " ORDER BY url, arm",
        (experiment_id, arm) if arm else (experiment_id,),
    )
    return ExperimentDetail(**row.model_dump(), results=results)
