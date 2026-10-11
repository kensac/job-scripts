"""Filter run permission, admission, and task status."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import budget, db, filter_runs, task_admission
from api.auth import AuthedUser, is_admin, require_user
from api.config import group_access_allowed
from api.problem import refuse

router = APIRouter()

_BLOCKED = budget.AccessReason | Literal["DEFERRED"]


class RunQueued(BaseModel):
    task_id: int | None


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
    return is_admin(user.groups) or group_access_allowed(RUN_GROUPS_KEY, user.groups)


def _refuse_unpermitted_run(user: AuthedUser) -> None:
    if not _may_run_by_hand(user):
        raise refuse(403, "NOT_PERMITTED", NOT_PERMITTED_MESSAGE)


def _enqueue_on_change(user: AuthedUser, filter_id: int) -> tuple[int | None, _BLOCKED | None]:
    """A save that would run the filter: runs it when the person's group
    re-judges on change, otherwise says the sweep will (Kanishk, 2026-09-09:
    stop recomputing whenever a filter changes, behind a flag)."""
    if not _rejudge_on_change(user):
        return None, "DEFERRED"
    return _enqueue_filter_run(user, filter_id, defer_conflict=True)


def _blocked_message(user: AuthedUser, blocked: _BLOCKED) -> str:
    if blocked == "DEFERRED":
        return DEFERRED_MESSAGE
    return budget.access_message(blocked, budget.get_entitlement(user))


def _in_flight_filter_task(
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
        raise refuse(409, "IN_PROGRESS", "this run is already in progress", task_id=running.id)


def _enqueue_filter_run(
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


@router.post("/user/filters/{filter_id}/run")
def run_filter(filter_id: int, user: AuthedUser = Depends(require_user)) -> RunQueued:
    _refuse_unpermitted_run(user)
    if not db.query_one(
        "SELECT id FROM user_filters WHERE id = %s AND user_id = %s", (filter_id, user.id)
    ):
        raise refuse(404, "NOT_FOUND", "unknown filter")
    task_id, blocked = _enqueue_filter_run(user, filter_id)
    if blocked:
        raise refuse(402, blocked, _blocked_message(user, blocked))
    return RunQueued(task_id=task_id)


@router.post("/user/filters/run-all")
def run_all_filters(user: AuthedUser = Depends(require_user)) -> RunQueued:
    _refuse_unpermitted_run(user)
    task_id, blocked = _enqueue_filter_run(user, None)
    if blocked:
        raise refuse(402, blocked, _blocked_message(user, blocked))
    return RunQueued(task_id=task_id)
