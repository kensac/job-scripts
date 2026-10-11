"""Filter insight views scoped to the caller's current prompt versions."""

from __future__ import annotations

import collections
from typing import Any

from fastapi import APIRouter, Depends, Query

from api import db
from api.auth import AuthedUser, require_user
from api.routers.filtering.insight_breakdowns import (
    DECISION_WINDOW_SQL,
    DEFAULT_MIN_DECISIONS,
    UNGROUPED_REASON_KEY,
    MyPromptVersion,
    MyRejectionReasons,
    OwnedFilter,
    PromptTotals,
    ReasonBreakdownAccumulator,
    _evidence_criterion,
    _example_selection,
    _group_rejections_by_prompt,
    _named_group_breakdowns,
    _prompt_totals,
)

user_router = APIRouter(prefix="/user/filter-insights")

# What the caller's own filters rejected, in the same vocabulary as the admin
# view and scoped to prompts they hold.
#
# Scoped by prompt_hash, not by user id, and that is deliberate: a custom
# verdict keys on (url, check_type, prompt_hash) and carries no user, so
# `prompt_hash IN (my filters)` is exactly the set the board's own visibility
# predicate uses. It is therefore the set that explains what the caller SEES.
# Two people running the same preset share those verdicts - one may be reading
# evaluations the other paid for - which is correct here, because they are
# facts about public postings rather than about either person.
#
# `ai_queries.filter_name` is NEVER returned. It embeds the owner as
# `user1:pay_tier_200`, so echoing it would leak another user's id and filter
# names to anyone who adopted the same preset. Names come from the caller's
# own user_filters rows instead.
_USER_HASHES_SQL = """
SELECT prompt_hash, name, enabled FROM user_filters
WHERE user_id = %(uid)s AND prompt_hash IS NOT NULL ORDER BY name
"""


@user_router.get("/rejection-reasons")
def my_rejection_reasons(
    days: int = Query(30, ge=1, le=365),
    min_decisions: int = Query(DEFAULT_MIN_DECISIONS, ge=1),
    examples: int = Query(3, ge=0, le=5),
    user: AuthedUser = Depends(require_user),
) -> MyRejectionReasons:
    """Why your filters rejected what they rejected.

    The admin view answers this across everyone, which means the person who
    can actually fix a misfiring filter - its owner - could not see it. Same
    taxonomy and same floors, so the two surfaces cannot describe the same
    prompt differently.
    """
    mine = db.query(_USER_HASHES_SQL, {"uid": user.id})
    if not mine:
        return MyRejectionReasons(
            window_days=days,
            min_decisions=min_decisions,
            examples_selection=_example_selection(),
            evidence_missing_criterion=_evidence_criterion(),
            prompt_versions=[],
        )

    # A hash can carry several of the caller's own filter names - one prompt
    # here is both "default" and "general" - so the row is keyed on the prompt
    # and lists the names, rather than pretending to be one filter.
    names_for: dict[str, list[OwnedFilter]] = collections.defaultdict(list)
    for row in mine:
        names_for[row["prompt_hash"]].append(OwnedFilter(name=row["name"], enabled=row["enabled"]))
    hashes = sorted(names_for)
    params: dict[str, Any] = {"days": days, "hashes": hashes}

    totals = db.query(
        f"""
        SELECT prompt_hash,
               count(*) AS evaluated,
               count(*) FILTER (WHERE status = 'passed') AS passed,
               count(*) FILTER (WHERE status = 'rejected') AS rejected,
               count(*) FILTER (
                   WHERE status = 'rejected' AND reason IS NOT NULL AND reason <> ''
               ) AS rejected_with_reason,
               count(DISTINCT url) AS distinct_jobs_evaluated,
               count(DISTINCT url) FILTER (WHERE status = 'rejected')
                   AS distinct_jobs_rejected,
               min(created_at) AS first_seen,
               max(created_at) AS last_seen
        FROM ai_queries
        WHERE check_type = 'custom' AND prompt_hash = ANY(%(hashes)s) AND {DECISION_WINDOW_SQL}
        GROUP BY 1
        """,
        params,
    )
    rejections = db.query(
        f"""
        SELECT prompt_hash, url, reason FROM verdicts
        WHERE check_type = 'custom' AND status = 'rejected'
          AND prompt_hash = ANY(%(hashes)s)
          AND reason IS NOT NULL AND reason <> '' AND {DECISION_WINDOW_SQL}
        """,
        params,
    )
    grouped, per_hash = _group_rejections_by_prompt(hashes, rejections)

    seen = {row["prompt_hash"] for row in totals}
    out = []
    for row in totals:
        h = row["prompt_hash"]
        buckets = grouped[h]
        missing = per_hash[h]
        out.append(
            MyPromptVersion(
                prompt_hash=h,
                filters=names_for[h],
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                sufficient=row["rejected"] >= min_decisions,
                totals=_prompt_totals(row, missing),
                groups=_named_group_breakdowns(buckets, examples),
                ungrouped=buckets.get(
                    UNGROUPED_REASON_KEY, ReasonBreakdownAccumulator()
                ).to_breakdown(examples),
            )
        )
    # A filter that decided nothing in the window is still the caller's, and
    # showing nothing at all would read as "no problems" rather than "nothing
    # ran". Zero rows say which.
    for h in hashes:
        if h not in seen:
            out.append(
                MyPromptVersion(
                    prompt_hash=h,
                    filters=names_for[h],
                    first_seen=None,
                    last_seen=None,
                    sufficient=False,
                    totals=PromptTotals(
                        evaluated=0,
                        passed=0,
                        rejected=0,
                        rejected_with_reason=0,
                        distinct_jobs_evaluated=0,
                        distinct_jobs_rejected=0,
                        evidence_missing_decisions=0,
                        evidence_missing_distinct_jobs=0,
                    ),
                    groups=[],
                    ungrouped=ReasonBreakdownAccumulator().to_breakdown(examples),
                )
            )
    out.sort(key=lambda r: r.totals.rejected, reverse=True)
    return MyRejectionReasons(
        window_days=days,
        min_decisions=min_decisions,
        examples_selection=_example_selection(),
        evidence_missing_criterion=_evidence_criterion(),
        prompt_versions=out,
    )
