"""Administrative filter insight views across prompt versions."""

from __future__ import annotations

import collections
from typing import Any

from fastapi import APIRouter, Depends, Query

from api import db, scoping
from api import params as params_
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin import require_admin
from api.routers.filtering.insight_breakdowns import (
    DECISION_WINDOW_SQL,
    DEFAULT_MIN_DECISIONS,
    UNGROUPED_REASON_KEY,
    OwnedFilter,
    OwnerUser,
    Phrasing,
    PhrasingPage,
    PromptOwner,
    PromptVersion,
    ReasonBreakdownAccumulator,
    ReasonGroup,
    RejectionReasons,
    Taxonomy,
    _evidence_criterion,
    _example_selection,
    _group_rejections_by_prompt,
    _named_group_breakdowns,
    _prompt_totals,
)
from core import reason_taxonomy
from core.reason_taxonomy import GROUP_LABELS, GROUPS

router = APIRouter(prefix="/admin/filter-insights")


@router.get("/groups")
def groups(user: AuthedUser = Depends(require_admin)) -> Taxonomy:
    """The taxonomy itself: keys, labels, and what evidence_missing means.

    A drill-through link carries only the key, so the page it lands on has to
    turn that back into words. Resolving it here rather than passing display
    text through the URL keeps one definition of a label - a link is a durable
    thing and a label baked into one goes stale the moment the taxonomy is
    edited, while a humanised key silently loses whatever the label said that
    the key does not. `seniority` covers new-grad and first-year mismatches,
    not only over-seniority, and `location` covers work authorisation.
    """
    return Taxonomy(
        groups=[ReasonGroup(key=g.key, label=GROUP_LABELS[g.key]) for g in GROUPS],
        evidence_missing_criterion=_evidence_criterion(),
    )


@router.get("/phrasings")
def phrasings(
    prompt_hash: str,
    group: str | None = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    users: str | None = Query(default=None, alias="user"),
    user: AuthedUser = Depends(require_admin),
) -> PhrasingPage:
    """Every distinct phrasing in one group of one prompt version, with counts.

    The counts are the point. "159 distinct phrasings" answers a different
    question depending on whether it is three sentences repeated fifty times
    or 159 near-unique ones, and in this corpus it is overwhelmingly the
    latter - so the distribution is the evidence that the grouping is doing
    real work rather than collapsing text that was already identical.

    Scoped by prompt_hash like everything else here, because a phrasing count
    that spanned prompt versions would not match the group count it was opened
    from.
    """
    where = [
        "check_type = 'custom'",
        "status = 'rejected'",
        "reason IS NOT NULL",
        "reason <> ''",
        "prompt_hash = %(prompt_hash)s",
        DECISION_WINDOW_SQL,
    ]
    params: dict[str, Any] = {"prompt_hash": prompt_hash, "days": days}
    ids = scoping.user_ids(users)
    if ids:
        where.append(scoping.filters_of())
        params["user_ids"] = ids
    if group is not None:
        if group == UNGROUPED_REASON_KEY:
            # The residual is defined by matching nothing, so it is the
            # conjunction of every pattern's negation rather than a pattern.
            for g in GROUPS:
                where.append(f"reason !~* %(ng_{g.key})s")
                params[f"ng_{g.key}"] = reason_taxonomy.sql_pattern(g.key)
        elif group in GROUP_LABELS:
            where.append("reason ~* %(group)s")
            params["group"] = reason_taxonomy.sql_pattern(group)
        else:
            raise refuse(
                400,
                "UNKNOWN_REASON_GROUP",
                f"unknown group: {group}",
                valid=[g.key for g in GROUPS] + [UNGROUPED_REASON_KEY],
            )
    clause = " AND ".join(where)

    totals = db.query_one(
        f"SELECT count(DISTINCT reason) AS phrasings, count(*) AS decisions "
        f"FROM ai_queries WHERE {clause}",
        params,
    )
    rows = db.query_as(
        Phrasing,
        f"""
        SELECT reason AS phrasing, count(*) AS decisions,
               count(DISTINCT url) AS distinct_jobs
        FROM ai_queries WHERE {clause}
        GROUP BY reason
        ORDER BY count(*) DESC, reason
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {**params, "limit": limit, "offset": offset},
    )
    total_phrasings = (totals or {}).get("phrasings", 0)
    return PhrasingPage(
        prompt_hash=prompt_hash,
        group=group,
        window_days=days,
        total_phrasings=total_phrasings,
        total_decisions=(totals or {}).get("decisions", 0),
        offset=offset,
        limit=limit,
        returned=len(rows),
        # Stated rather than implied: a caller must be able to say "showing 50
        # of 159" instead of silently presenting a page as the whole set.
        has_more=offset + len(rows) < total_phrasings,
        phrasings=rows,
        filters=params_.applied(
            prompt_hash=[prompt_hash], group=params_.csv(group), user=scoping.echo(ids)
        ),
        filterable=["prompt_hash", "group", "user"],
    )


@router.get("/rejection-reasons")
def rejection_reasons(
    days: int = Query(30, ge=1, le=365),
    prompt_hash: str | None = None,
    min_decisions: int = Query(DEFAULT_MIN_DECISIONS, ge=1),
    examples: int = Query(3, ge=0, le=5),
    users: str | None = Query(default=None, alias="user"),
    user: AuthedUser = Depends(require_admin),
) -> RejectionReasons:
    params: dict[str, Any] = {"days": days}
    hash_clause = ""
    if prompt_hash:
        hash_clause = " AND prompt_hash = %(prompt_hash)s"
        params["prompt_hash"] = prompt_hash
    ids = scoping.user_ids(users)
    if ids:
        hash_clause += " AND " + scoping.filters_of()
        params["user_ids"] = ids
    applied = params_.applied(prompt_hash=params_.csv(prompt_hash), user=scoping.echo(ids))
    filterable = ["prompt_hash", "user"]

    # Reads only columns `idx_ai_queries_custom_window` carries, so the
    # 740,000 verdicts of a 30-day window are an index-only scan rather than a
    # walk of 1.5 GB of heap. `rejected_with_reason` needs `reason`, so it is
    # counted from the rejection rows below. The distincts sort in "C": two
    # strings are equal under en_US exactly when their bytes are, so the
    # counts are the same, and the url sort took 2.5-3 s against 18 s
    # (production, 2026-11-01).
    totals = db.query(
        f"""
        SELECT prompt_hash,
               count(*) AS evaluated,
               count(*) FILTER (WHERE status = 'passed') AS passed,
               count(*) FILTER (WHERE status = 'rejected') AS rejected,
               count(DISTINCT url COLLATE "C") AS distinct_jobs_evaluated,
               count(DISTINCT url COLLATE "C") FILTER (WHERE status = 'rejected')
                   AS distinct_jobs_rejected,
               min(created_at) AS first_seen,
               max(created_at) AS last_seen,
               array_remove(array_agg(DISTINCT filter_name COLLATE "C"), NULL) AS filter_names
        FROM ai_queries
        WHERE check_type = 'custom' AND prompt_hash IS NOT NULL
          AND {DECISION_WINDOW_SQL}{hash_clause}
        GROUP BY 1
        """,
        params,
    )
    if not totals:
        return RejectionReasons(
            window_days=days,
            min_decisions=min_decisions,
            examples_selection=_example_selection(),
            evidence_missing_criterion=_evidence_criterion(),
            prompt_versions=[],
            filters=applied,
            filterable=filterable,
        )

    # Exactly the predicate of `idx_ai_queries_custom_reasons`, an index-only
    # scan. It reads ai_queries rather than the verdicts view, whose extra
    # predicates this one implies.
    rejections = db.query(
        f"""
        SELECT prompt_hash, url, reason FROM ai_queries
        WHERE check_type = 'custom' AND status = 'rejected' AND reason <> ''
          AND prompt_hash IS NOT NULL AND {DECISION_WINDOW_SQL}{hash_clause}
        """,
        params,
    )
    reasoned = collections.Counter(r["prompt_hash"] for r in rejections)

    hashes = [r["prompt_hash"] for r in totals]
    # One person can hold SEVERAL filters carrying the same prompt text - one
    # user in this database has two - so a row per user_filters row is not a
    # row per owner. Counting rows reported that prompt as shared between two
    # people when it belongs to one, which is a claim about who to go and talk
    # to. Users are collapsed by sub; the filter rows are reported separately.
    owners: dict[str, dict[str, OwnerUser]] = collections.defaultdict(dict)
    filters_for: dict[str, list[OwnedFilter]] = collections.defaultdict(list)
    for row in db.query(
        """
        SELECT f.prompt_hash, f.name, f.enabled, u.sub, u.email FROM user_filters f
        JOIN users u ON u.id = f.user_id WHERE f.prompt_hash = ANY(%s)
        ORDER BY f.name
        """,
        (hashes,),
    ):
        owners[row["prompt_hash"]][row["sub"]] = OwnerUser(sub=row["sub"], email=row["email"] or "")
        filters_for[row["prompt_hash"]].append(
            OwnedFilter(name=row["name"], enabled=row["enabled"])
        )

    # A name that appears under more than one hash in this window spans a
    # prompt edit; the caller needs to know before it presents them as one row.
    by_name: dict[str, set[str]] = collections.defaultdict(set)
    for row in totals:
        for name in row["filter_names"]:
            by_name[name].add(row["prompt_hash"])

    grouped, per_hash = _group_rejections_by_prompt(hashes, rejections)

    out = []
    for row in totals:
        h = row["prompt_hash"]
        buckets = grouped[h]
        # Per name, not a single number: a hash can carry several names (one
        # prompt has been "default", "general" and "user1:default"), and a
        # scalar would leave the caller unable to say which name it describes.
        siblings_by_name = {name: len(by_name[name] - {h}) for name in sorted(row["filter_names"])}
        missing = per_hash[h]
        people = list(owners.get(h, {}).values())
        current = filters_for.get(h, [])
        out.append(
            PromptVersion(
                prompt_hash=h,
                filter_names=sorted(row["filter_names"]),
                sibling_hashes_by_name=siblings_by_name,
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                owner=PromptOwner(
                    state="resolved"
                    if len(people) == 1
                    else ("shared" if len(people) > 1 else "unknown"),
                    user_count=len(people),
                    users=people,
                    # Whether this prompt can still reject anything. `resolved`
                    # does not answer it: a disabled filter is still a current
                    # row, so a retired prompt resolves to its owner exactly
                    # like a live one. Without this a caller ranking by misfire
                    # rate leads with a filter that cannot fire, spending the
                    # reader's attention on the one thing they cannot act on.
                    # None when no current filter carries this prompt at all,
                    # because then there is nothing to ask.
                    enabled=any(f.enabled for f in current) if current else None,
                    filters=current,
                ),
                sufficient=row["rejected"] >= min_decisions,
                totals=_prompt_totals(row, missing, reasoned[h]),
                groups=_named_group_breakdowns(buckets, examples),
                ungrouped=buckets.get(
                    UNGROUPED_REASON_KEY, ReasonBreakdownAccumulator()
                ).to_breakdown(examples),
            )
        )

    out.sort(key=lambda r: r.totals.rejected, reverse=True)
    return RejectionReasons(
        window_days=days,
        min_decisions=min_decisions,
        examples_selection=_example_selection(),
        evidence_missing_criterion=_evidence_criterion(),
        prompt_versions=out,
        filters=applied,
        filterable=filterable,
    )
