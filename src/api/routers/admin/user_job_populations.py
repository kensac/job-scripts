"""Named user-job pair populations, without choosing a legacy replacement."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import params as params_
from api import scoping
from api.auth import AuthedUser
from api.board import populations
from api.routers.admin.shared import require_admin

router = APIRouter()


class PopulationCounts(BaseModel):
    source: str | None
    legacy_compatibility: int
    person_state: int
    working_set: int
    materialized_visibility: int
    person_and_working_set: int
    person_and_visibility: int
    working_set_and_visibility: int
    all_three: int
    person_shaped_unmarked: int
    legacy_unknown: int
    visibility_computed_at_min: datetime.datetime | None
    visibility_computed_at_max: datetime.datetime | None


class UserJobPopulations(BaseModel):
    generated_at: datetime.datetime
    basis: str = "distinct user_id/job_id pairs"
    visibility_basis: str = "materialized board_visible rows; timestamps describe rows present, not projection freshness"
    global_: PopulationCounts = Field(serialization_alias="global")
    by_source: list[PopulationCounts]
    filters: dict[str, list[str]]
    filterable: list[str]


def _view(row: populations.PopulationRow) -> PopulationCounts:
    return PopulationCounts(
        **{key: value for key, value in vars(row).items() if key != "generated_at"}
    )


@router.get("/user-job-populations")
def user_job_populations(
    user: str | None = None,
    source: str | None = None,
    admin: AuthedUser = Depends(require_admin),
) -> UserJobPopulations:
    """Report overlapping stored populations; do not infer one from another."""
    user_ids = scoping.user_ids(user)
    sources = params_.csv(source)
    rows = populations.read(user_ids=user_ids, sources=sources)
    return UserJobPopulations(
        generated_at=rows[0].generated_at,
        global_=_view(rows[0]),
        by_source=[_view(row) for row in rows[1:]],
        filters={"user": scoping.echo(user_ids), "source": sources},
        filterable=["user", "source"],
    )
