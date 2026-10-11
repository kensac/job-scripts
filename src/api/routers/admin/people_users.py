"""Administrative user ledger and per-person detail."""

from __future__ import annotations

import datetime
import logging
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import db, model_calls, scoping, sorting, user_settings
from api.auth import AuthedUser
from api.board import populations
from api.problem import refuse
from api.routers.admin.shared import require_admin

router = APIRouter()


logger = logging.getLogger(__name__)


# Whitelisted sort keys for the users ledger; the SQL names the computed
# columns of the query below, so a key cannot reach anything the row does not
# already show.
_USERS_SORTABLE = {
    "last_seen_at": "u.last_seen_at",
    "created_at": "u.created_at",
    "email": "lower(u.email)",
    "name": "lower(u.name)",
    "acted_on": "acted_on",
    "working_set": "working_set",
    "visible": "visible",
    "enabled_filters": "enabled_filters",
    "sources": "sources",
    "owner_tokens_week": "owner_tokens_week",
}


class UserLedgerRow(BaseModel):
    """One person, with the counts the ledger sorts on. Each is a
    correlated count rather than a join, so a person with no board rows and no
    filters still appears with zeros.

    `acted_on`, `working_set` and `visible` are the three labelled
    populations (phase 2b): postings the person acted on, postings their
    filters picked, postings they can see.

    `has_byo_key` says whether they hold their own provider key, which is what
    decides whose budget their runs come out of. The key itself never leaves
    the database.
    """

    id: int
    sub: str
    email: str | None
    name: str | None
    groups: list[str]
    created_at: datetime.datetime
    last_seen_at: datetime.datetime
    has_byo_key: bool
    ai_provider: str | None
    ai_model: str | None
    bypass_sponsorship_filter: bool | None
    acted_on: int
    working_set: int
    visible: int
    enabled_filters: int
    sources: int
    owner_tokens_week: int


class UserLedger(BaseModel):
    """`sort`, `dir` and `sorts` echo the sort that was applied and `sortable`
    enumerates the keys, so the page renders the active sort without
    duplicating the default and never has to guess what it may ask for."""

    users: list[UserLedgerRow]
    has_more: bool
    sort: str
    dir: str
    sorts: list[dict[str, str]]
    sortable: list[str]


@router.get("/users")
def list_users(
    limit: int = 50,
    offset: int = 0,
    sort: str = "last_seen_at",
    dir: str = "desc",
    ids: str | None = None,
    user: AuthedUser = Depends(require_admin),
) -> UserLedger:
    limit = max(1, min(limit, 200))
    sorts = sorting.parse(sort, dir, _USERS_SORTABLE, "last_seen_at")
    # ids= is a bulk lookup: the queue page names workers' tasks by user and
    # was walking up to 40 pages to build that map. Ids that are not integers
    # are ignored rather than refused, so a malformed selection returns what
    # it can.
    wanted = scoping.user_ids(ids)
    scope = "WHERE u.id = ANY(%(ids)s)" if ids is not None else ""
    rows = db.query_as(
        UserLedgerRow,
        f"""
        SELECT u.id, u.sub, u.email, u.name, u.groups, u.created_at, u.last_seen_at,
               s.api_key_enc IS NOT NULL AS has_byo_key,
               s.ai_provider, s.ai_model, s.bypass_sponsorship_filter,
               {populations.per_user_counts("u.id")},
               (SELECT COUNT(*) FROM user_filters uf
                WHERE uf.user_id = u.id AND uf.enabled) AS enabled_filters,
               (SELECT COUNT(*) FROM user_source_set us WHERE us.user_id = u.id) AS sources,
               COALESCE((SELECT SUM(a.total_tokens) FROM model_calls a
                         WHERE a.user_id = u.id AND a.key_source = 'owner'
                           AND a.created_at > now() - interval '7 days'), 0) AS owner_tokens_week
        FROM users u {user_settings.join("s", "u.id")}
        {scope}
        ORDER BY {sorting.clause(sorts, _USERS_SORTABLE)}, u.id LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"limit": limit + 1, "offset": max(0, offset), "ids": wanted},
    )
    return UserLedger(
        users=rows[:limit],
        has_more=len(rows) > limit,
        # Echoed and enumerated for the same reason as /admin/jobs: the page
        # renders the active sort without duplicating the default and never
        # has to guess the accepted keys.
        sort=sorts[0]["key"],
        dir=sorts[0]["dir"],
        sorts=sorts,
        sortable=sorted(_USERS_SORTABLE),
    )


class UserProfile(BaseModel):
    """A person and their settings, minus anything secret. `criteria` and
    `ai_params` are shaped by what the person saved, so neither is declared
    further."""

    id: int
    sub: str
    email: str | None
    name: str | None
    groups: list[str]
    created_at: datetime.datetime
    last_seen_at: datetime.datetime
    ai_provider: str | None
    ai_model: str | None
    ai_params: dict[str, Any] | None
    bypass_sponsorship_filter: bool | None
    criteria: dict[str, Any] | None
    has_byo_key: bool


class GroupGrant(BaseModel):
    """A group this person is in that carries a budget. A null budget is
    uncapped, which is a real answer and not a missing one."""

    group_name: str
    weekly_token_budget: int | None


class UserBudget(BaseModel):
    """Which cap applies to this person, resolved here rather than left to the
    page. The Users page showed a weekly total one click from a page showing a
    5,000,000 budget with nothing saying which cap applied to whom, which
    invited the reading that an uncapped owner was over budget.

    `owner_key` is whether they spend on the shared key at all; somebody on
    their own key has no cap to be under.
    """

    owner_key: bool
    weekly_token_budget: int | None
    spent_this_week: int
    granted_by: list[GroupGrant]


class SpendDay(BaseModel):
    day: datetime.date
    # Null: the call's record did not say whose key.
    key_source: str | None
    tokens: int
    calls: int


class SpendPurpose(BaseModel):
    purpose: str
    model: str | None
    tokens: int
    calls: int


class BoardStatusCount(BaseModel):
    """`status` is 'not_applied' where the row has none, so every board row is
    counted under some heading."""

    status: str
    count: int
    hidden: int


class UserFilterRow(BaseModel):
    """A person's filters, without their prompts: this page is about who they
    are, and the prompt is theirs to read on their own screen."""

    id: int
    name: str
    enabled: bool
    on_ambiguous: str
    fail_closed: bool
    updated_at: datetime.datetime


class UploadCounts(BaseModel):
    total: int
    failed: int


class ReportCounts(BaseModel):
    open: int
    total: int


class RecentTask(BaseModel):
    id: int
    kind: str
    status: str
    worker: str | None
    created_at: datetime.datetime
    finished_at: datetime.datetime | None


class UserDetail(BaseModel):
    """Everything one person's page shows: who they are, what they may spend
    and what they have spent, what their board and filters look like, and the
    last ten things the fleet did for them."""

    user: UserProfile
    budget: UserBudget
    spend_by_day: list[SpendDay]
    spend_by_purpose: list[SpendPurpose]
    board: list[BoardStatusCount]
    filters: list[UserFilterRow]
    sources: list[str]
    uploads: UploadCounts
    reports: ReportCounts
    recent_tasks: list[RecentTask]


@router.get("/users/{user_id}")
def user_detail(user_id: int, user: AuthedUser = Depends(require_admin)) -> UserDetail:
    profile = db.query_one_as(
        UserProfile,
        f"""
        SELECT u.id, u.sub, u.email, u.name, u.groups, u.created_at, u.last_seen_at,
               s.ai_provider, s.ai_model, s.ai_params, s.bypass_sponsorship_filter,
               s.criteria, s.api_key_enc IS NOT NULL AS has_byo_key
        FROM users u {user_settings.join("s", "u.id")}
        WHERE u.id = %s
        """,
        (user_id,),
    )
    if not profile:
        raise refuse(404, "NOT_FOUND", "unknown user")
    from api import budget as _budget

    # The Users page showed a weekly token total one click from a page showing
    # a 5,000,000 budget, with nothing saying which cap applied to whom - which
    # invited the reading that the owner was over budget when his group is
    # uncapped. Resolve it here rather than leaving the UI to infer.
    member_groups = profile.groups
    owner_key, weekly_cap = _budget._owner_budget(member_groups)
    uploads = db.query_one_as(
        UploadCounts,
        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE status = 'failed') AS failed "
        "FROM posting_uploads WHERE uploaded_by = %s",
        (user_id,),
    )
    reports = db.query_one_as(
        ReportCounts,
        "SELECT COUNT(*) FILTER (WHERE status = 'open') AS open, COUNT(*) AS total "
        "FROM reports WHERE user_id = %s",
        (user_id,),
    )
    # Aggregates with no GROUP BY, so each is exactly one row.
    assert uploads is not None and reports is not None
    return UserDetail(
        user=profile,
        budget=UserBudget(
            owner_key=owner_key,
            # None means uncapped, which is a real answer and not a missing one.
            weekly_token_budget=weekly_cap,
            spent_this_week=_budget.spent_this_week(user_id) if owner_key else 0,
            granted_by=db.query_as(
                GroupGrant,
                "SELECT group_name, weekly_token_budget FROM group_budgets "
                "WHERE group_name = ANY(%s) ORDER BY group_name",
                (member_groups,),
            ),
        ),
        spend_by_day=[SpendDay(**r) for r in model_calls.user_spend_by_day(user_id)],
        spend_by_purpose=[SpendPurpose(**r) for r in model_calls.user_spend_by_purpose(user_id)],
        board=db.query_as(
            BoardStatusCount,
            """
            SELECT COALESCE(NULLIF(status, ''), 'not_applied') AS status,
                   COUNT(*) AS count, COUNT(*) FILTER (WHERE hidden) AS hidden
            FROM user_jobs WHERE user_id = %s GROUP BY 1 ORDER BY 2 DESC
            """,
            (user_id,),
        ),
        filters=db.query_as(
            UserFilterRow,
            "SELECT id, name, enabled, on_ambiguous, fail_closed, updated_at "
            "FROM user_filters WHERE user_id = %s ORDER BY id",
            (user_id,),
        ),
        sources=[
            r["source"]
            for r in db.query(
                "SELECT source FROM user_source_set WHERE user_id = %s ORDER BY source",
                (user_id,),
            )
        ],
        uploads=uploads,
        reports=reports,
        recent_tasks=db.query_as(
            RecentTask,
            """
            SELECT id, kind, status, worker, created_at, finished_at
            FROM tasks WHERE payload->>'user_id' = %s ORDER BY id DESC LIMIT 10
            """,
            (str(user_id),),
        ),
    )
