"""One queue for everything awaiting a human decision, and one way to answer it.

Six resolve/undo pairs existed across six URL shapes with six payloads, each
individually well built. Together they were why the unmatched queue offered a
single verb - "say where this belongs" - when the honest answer is often
neither an application nor a correction.

The CHOICES ARE DECLARED BY THE SERVER. A row carries what it is, the evidence
behind it, and the verbs available on it, so the picker renders buttons from
data and a new decision type needs no frontend change. That is the part of
this that is architecture rather than plumbing.

Every answer APPENDS. Undo is another append, the wrong answer stays visible,
and `actor_user_id` records who decided - the same contract corrections got,
rather than a second one.

FOUR KINDS, ONE QUEUE. Unmatched mail was the only kind for as long as it was
the only one anybody could answer. The other three were already produced and
already unanswered: 4,674 attachments the matcher made with nobody ever asked
whether they were right, 1,159 status proposals on offer against 0 answers,
and 525 open action items. They were reachable through four more endpoint
families, which is the same shape this module was written to collapse, so they
belong here rather than beside it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import AuthedUser, require_user
from api.resolve.choice_policy import by_company as by_company
from api.resolve.choice_policy import choices_for_message as choices_for_message
from api.resolve.choice_policy import thread_size as thread_size
from api.resolve.commands import resolve as resolve_command
from api.resolve.contracts import (
    ITEM_KINDS,
    DecisionHistory,
    ResolveQueue,
    ResolveRequest,
    ResolveResult,
    ReviewRates,
)
from api.resolve.contracts import PICKER_APPLICATIONS as PICKER_APPLICATIONS
from api.resolve.contracts import ResolveChoice as ResolveChoice
from api.resolve.history import history_for
from api.resolve.queue import queue_for
from api.resolve.review_rates import review_rates_for
from api.routers.admin import require_admin

router = APIRouter()


@router.get("/user/resolve/queue", response_model=ResolveQueue, response_model_exclude_none=True)
def resolve_queue(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    kind: list[str] | None = Query(default=None),
    user: AuthedUser = Depends(require_user),
):
    """Everything of the user's own awaiting a decision, of every kind.

    `kind` narrows it, repeated for several. Unfiltered is the default because
    "what is waiting on me" is the question this answers, and four separate
    answers to it is the shape it replaced.
    """
    if kind and set(kind) - set(ITEM_KINDS):
        raise HTTPException(
            400,
            detail={
                "code": "UNKNOWN_KIND",
                "message": f"kind must be one of {', '.join(ITEM_KINDS)}",
            },
        )
    return queue_for(user.id, limit, offset, kind)


@router.get(
    "/user/resolve/history", response_model=DecisionHistory, response_model_exclude_none=True
)
def resolve_history(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_user),
):
    """What the user has decided, newest first, overturned answers included."""
    return history_for(user.id, user.id, limit, offset)


@router.post(
    "/user/resolve/{item_id}", response_model=ResolveResult, response_model_exclude_none=True
)
def resolve_item(
    item_id: str, body: ResolveRequest, user: AuthedUser = Depends(require_user)
) -> ResolveResult:
    return resolve_command(item_id, body, owner_id=user.id, actor_user_id=user.id)


@router.get("/admin/resolve/queue", response_model=ResolveQueue, response_model_exclude_none=True)
def admin_resolve_queue(
    user_id: int = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    kind: list[str] | None = Query(default=None),
    user: AuthedUser = Depends(require_admin),
):
    """The same queue over another user's mail. Owner is a parameter; the
    caller's identity decides only whether they may ask."""
    return queue_for(user_id, limit, offset, kind)


@router.get(
    "/admin/resolve/history", response_model=DecisionHistory, response_model_exclude_none=True
)
def admin_resolve_history(
    user_id: int = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthedUser = Depends(require_admin),
):
    """One user's decisions, as an administrator sees them.

    `by` is computed against the CALLER, so the administrator's own
    corrections read as "you" and the owner's read as somebody else - the
    opposite of what the owner sees for the same rows, and correct for both.
    """
    return history_for(user_id, user.id, limit, offset)


@router.post(
    "/admin/resolve/{item_id}", response_model=ResolveResult, response_model_exclude_none=True
)
def admin_resolve_item(
    item_id: str,
    body: ResolveRequest,
    user_id: int = Query(...),
    user: AuthedUser = Depends(require_admin),
) -> ResolveResult:
    return resolve_command(item_id, body, owner_id=user_id, actor_user_id=user.id)


@router.get("/admin/resolve/rates", response_model=ReviewRates, response_model_exclude_none=True)
def admin_review_rates(
    user_id: int | None = Query(default=None), user: AuthedUser = Depends(require_admin)
):
    """How often a person agrees with each tier, and how much nobody has read.

    `user_id` is OPTIONAL and omitting it means the whole fleet. /job-scripts
    is the view across all users rather than one user's data with a permission
    level on it, so "is `ats_company` right" is the question it exists to ask -
    and a required owner made the fleet-wide form of it unaskable, which is the
    only form that decides whether a tier keeps writing unattended.

    Read-only, and deliberately without the verbs. An administrator answering
    somebody else's match is a different act from the owner answering it, and
    it already has a home on the resolve routes above where `actor_user_id`
    records which of the two happened. What this surface is for is deciding
    whether a tier should keep writing unattended, which is a question about
    the aggregate rather than about any row.
    """
    return review_rates_for(user_id)
