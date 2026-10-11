"""Persisted group budgets and model permissions."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser
from api.models import Ok
from api.problem import refuse
from api.routers.admin.shared import require_admin

router = APIRouter()


class GroupBudgetPut(BaseModel):
    weekly_token_budget: int | None = Field(default=None, ge=0)
    allowed_models: list[str] | None = Field(default=None, max_length=50)


class GroupBudget(BaseModel):
    """What a group may spend on the shared key. A null budget is uncapped and
    null allowed_models is every model in the catalog; both are real answers
    rather than unset ones."""

    group_name: str
    weekly_token_budget: int | None
    allowed_models: list[str] | None


class GroupBudgets(BaseModel):
    """`catalog_models` is every model the server can run, so the form offers
    a closed list rather than a text box that fails on save."""

    groups: list[GroupBudget]
    catalog_models: list[str]


@router.get("/group-budgets")
def list_group_budgets(user: AuthedUser = Depends(require_admin)) -> GroupBudgets:
    from api import ai

    return GroupBudgets(
        groups=db.query_as(
            GroupBudget,
            "SELECT group_name, weekly_token_budget, allowed_models "
            "FROM group_budgets ORDER BY group_name",
        ),
        catalog_models=sorted(m["model"] for models in ai.MODEL_CATALOG.values() for m in models),
    )


@router.put("/group-budgets/{group_name}")
def put_group_budget(
    group_name: str, body: GroupBudgetPut, user: AuthedUser = Depends(require_admin)
) -> GroupBudget:
    from api import ai

    if body.allowed_models is not None:
        known = {m["model"] for models in ai.MODEL_CATALOG.values() for m in models}
        unknown = [m for m in body.allowed_models if m not in known]
        if unknown:
            raise refuse(400, "UNKNOWN_MODEL", f"unknown models: {unknown}")
    db.execute(
        """
        INSERT INTO group_budgets (group_name, weekly_token_budget, allowed_models)
        VALUES (%s, %s, %s)
        ON CONFLICT (group_name) DO UPDATE SET
            weekly_token_budget = EXCLUDED.weekly_token_budget,
            allowed_models = EXCLUDED.allowed_models
        """,
        (group_name, body.weekly_token_budget, body.allowed_models),
    )
    return GroupBudget(
        group_name=group_name,
        weekly_token_budget=body.weekly_token_budget,
        allowed_models=body.allowed_models,
    )


@router.delete("/group-budgets/{group_name}")
def delete_group_budget(group_name: str, user: AuthedUser = Depends(require_admin)) -> Ok:
    db.execute("DELETE FROM group_budgets WHERE group_name = %s", (group_name,))
    return Ok()
