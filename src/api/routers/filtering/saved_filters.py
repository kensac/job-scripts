"""A person's filter rows, validation, and writes."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from psycopg.errors import UniqueViolation
from pydantic import BaseModel

from api import budget, db, filter_runs, task_admission
from api.auth import AuthedUser, require_user
from api.board import visibility
from api.models import FilterCreate, FilterPatch, Ok
from api.problem import refuse
from api.routers.filtering import run_controls as runs
from core.filters import ON_AMBIGUOUS_VALUES, compute_filter_hash

router = APIRouter()

FILTER_COLS = (
    "id, name, prompt, on_ambiguous, fail_closed, enabled, prompt_hash, preset_id, "
    "created_at, updated_at"
)


class Filter(BaseModel):
    """`FILTER_COLS`, in types. `preset_id` names the preset it was adopted
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


def _validate_ambiguous(value: str) -> str:
    if value not in ON_AMBIGUOUS_VALUES:
        raise refuse(
            400, "INVALID_ON_AMBIGUOUS", f"on_ambiguous must be one of {ON_AMBIGUOUS_VALUES}"
        )
    return value


def refuse_second_enabled(user_id: int, except_id: int | None = None) -> None:
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
        raise refuse(
            409,
            "ONE_FILTER",
            f'One filter runs at a time, and "{other["name"]}" is on. '
            "Turn it off first, or fold this into it: one prompt can hold "
            "every condition you want.",
            enabled_filter_id=other["id"],
        )


@router.get("/user/filters")
def list_filters(user: AuthedUser = Depends(require_user)) -> FilterList:
    """Each filter carries its in-flight run, and the list carries the
    in-flight run-all, so a button is disabled from server state."""
    rows = db.query_as(
        Filter,
        f"SELECT {FILTER_COLS} FROM user_filters WHERE user_id = %s ORDER BY id",
        (user.id,),
    )
    access_failure = budget.access_failure(user)
    may_run = runs._may_run_by_hand(user)
    return FilterList(
        filters=[
            FilterRow(
                **row.model_dump(),
                task=runs._in_flight_filter_task(user.id, "run_filter", row.id),
                run_admission=filter_runs.admission(
                    user.id, row.id, access_failure=access_failure
                ).decision(),
            )
            for row in rows
        ],
        run_all_task=runs._in_flight_filter_task(user.id, "run_all_filters"),
        run_all_admission=filter_runs.admission(
            user.id, None, access_failure=access_failure
        ).decision(),
        may_run_by_hand=may_run,
        may_run_message=None if may_run else runs.NOT_PERMITTED_MESSAGE,
    )


def after_filter_change(user: AuthedUser, row: Filter, previous: Filter | None) -> SavedFilter:
    needs_judgement = row.enabled and (
        previous is None or not previous.enabled or row.prompt_hash != previous.prompt_hash
    )
    task_id, blocked = runs._enqueue_on_change(user, row.id) if needs_judgement else (None, None)
    visibility.request_refresh(user.id)
    return SavedFilter(
        **row.model_dump(),
        task_id=task_id,
        run_blocked=blocked,
        run_blocked_message=runs._blocked_message(user, blocked) if blocked else None,
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
            raise refuse(409, "DUPLICATE_NAME", "filter name already exists")
        if body.enabled:
            refuse_second_enabled(user.id)
        row = db.query_one_as(
            Filter,
            f"""
            INSERT INTO user_filters (user_id, name, prompt, on_ambiguous, fail_closed, enabled, prompt_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING {FILTER_COLS}
            """,
            (
                user.id,
                body.name,
                body.prompt,
                body.on_ambiguous,
                body.fail_closed,
                body.enabled,
                compute_filter_hash(body.prompt, body.on_ambiguous),
            ),
        )
        assert row is not None
    return after_filter_change(user, row, None)


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
            f"SELECT {FILTER_COLS} FROM user_filters WHERE id = %s AND user_id = %s",
            (filter_id, user.id),
        )
        if not existing:
            raise refuse(404, "NOT_FOUND", "unknown filter")
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise refuse(400, "EMPTY_PATCH", "no fields to update")
        if "on_ambiguous" in fields:
            _validate_ambiguous(fields["on_ambiguous"])
        if fields.get("enabled") and not existing.enabled:
            refuse_second_enabled(user.id, filter_id)
        prompt = fields.get("prompt", existing.prompt)
        on_ambiguous = fields.get("on_ambiguous", existing.on_ambiguous)
        fields["prompt_hash"] = compute_filter_hash(prompt, on_ambiguous)
        cols = ", ".join(f"{k} = %({k})s" for k in fields)
        try:
            row = db.query_one_as(
                Filter,
                f"UPDATE user_filters SET {cols}, updated_at = now() "
                f"WHERE id = %(fid)s AND user_id = %(uid)s RETURNING {FILTER_COLS}",
                {"fid": filter_id, "uid": user.id, **fields},
            )
        except UniqueViolation as exc:
            # create_filter pre-checks the name; renaming has to answer the same
            # way rather than letting user_filters_user_id_name_key escape as a 500.
            raise refuse(409, "DUPLICATE_NAME", "filter name already exists") from exc
        assert row is not None
    return after_filter_change(user, row, existing)


@router.delete("/user/filters/{filter_id}")
def delete_filter(filter_id: int, user: AuthedUser = Depends(require_user)) -> Ok:
    db.execute("DELETE FROM user_filters WHERE id = %s AND user_id = %s", (filter_id, user.id))
    visibility.request_refresh(user.id)
    return Ok()
