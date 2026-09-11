"""Why a filter rejects what it rejects.

Split out of admin.py rather than added to it, the same way spend.py was.

The unit here is a PROMPT VERSION, not a filter. `ai_queries` carries no
filter id - only `prompt_hash` and a free-text `filter_name` - and neither is a
stable identity: `pay_tier_200` has been two different prompts, and one prompt
has been called "default", "general" and "user1:default". Keying a row on
filter identity would therefore average two different filters together across
a prompt edit and look perfectly fine while doing it. Keying it on prompt_hash
makes that impossible by construction, and `sibling_hashes_by_name` tells a
caller, per name, when the name it is showing spans more than one version.

Ownership is only partly knowable and the payload says so rather than
guessing. `user_filters.prompt_hash` holds the CURRENT hash only, so editing a
prompt orphans every verdict the old one produced. Measured over the corpus,
52% of rejections resolve to exactly one owner, 19% to several users holding
identical filter text, and 29% to nobody at all - edited or deleted, with
nothing in the schema to say which.
"""

from __future__ import annotations

import collections
import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db, scoping
from api import params as params_
from api.auth import AuthedUser, require_user
from api.routers.admin import require_admin
from core import reason_taxonomy
from core.reason_taxonomy import (
    EVIDENCE_MISSING_DESCRIPTION,
    EVIDENCE_MISSING_PHRASES,
    GROUP_LABELS,
    GROUPS,
    classify,
    is_evidence_missing,
)

router = APIRouter(prefix="/admin/filter-insights")


# A share is only worth rendering if one decision cannot swing it by more than
# a couple of points; at two points that is 1/0.02 decisions. Callers get the
# threshold back in the response so the UI and the API draw the same line
# rather than each inventing one.
_MAX_SWING_PER_DECISION = 0.02
_DEFAULT_MIN_DECISIONS = int(1 / _MAX_SWING_PER_DECISION)

_WINDOW = "created_at >= now() - make_interval(days => %(days)s)"

# The residual bucket, named once. It is both the key in the response and the
# value `group` accepts on the phrasings listing, so a caller can drill into
# "matched nothing" the same way it drills into any named group. Cannot
# collide with a taxonomy key - those are all specific.
UNGROUPED = "ungrouped"


class EvidenceCriterion(BaseModel):
    """What "evidence missing" means, in the payload rather than in whoever
    explained it once."""

    method: str
    description: str
    phrases: list[str]


class ExamplesSelection(BaseModel):
    """How the examples were chosen, for the same reason."""

    method: str
    ordered_by: str
    description: str


class ReasonGroup(BaseModel):
    key: str
    label: str


class Taxonomy(BaseModel):
    groups: list[ReasonGroup]
    evidence_missing_criterion: EvidenceCriterion


class Phrasing(BaseModel):
    """One distinct rejection sentence, with how often it was written."""

    phrasing: str
    decisions: int
    distinct_jobs: int


class PhrasingPage(BaseModel):
    """`filters` echoes what narrowed this page, keyed by parameter name, and
    carries only the parameters that narrowed anything."""

    prompt_hash: str
    group: str | None
    window_days: int
    total_phrasings: int
    total_decisions: int
    offset: int
    limit: int
    returned: int
    has_more: bool
    phrasings: list[Phrasing]
    filters: dict[str, list[str]]
    filterable: list[str]


class Breakdown(BaseModel):
    """One bucket of rejections. `examples` is a reproducible sample of
    `distinct_phrasings`, not a frequency ranking; see `_finish`."""

    decisions: int
    distinct_jobs: int
    distinct_phrasings: int
    evidence_missing_decisions: int
    evidence_missing_distinct_jobs: int
    examples: list[str]


class GroupBreakdown(Breakdown):
    """A bucket that a taxonomy group named. The residual bucket is the same
    shape without the name, and rides on the row as `ungrouped`."""

    key: str
    label: str


class PromptTotals(BaseModel):
    """`rejected_with_reason` is the denominator the groups are of, which is
    not `rejected`: the batched paths recorded no reason for a stretch."""

    evaluated: int
    passed: int
    rejected: int
    rejected_with_reason: int
    distinct_jobs_evaluated: int
    distinct_jobs_rejected: int
    evidence_missing_decisions: int
    evidence_missing_distinct_jobs: int


class OwnedFilter(BaseModel):
    """A filter that carries this prompt today."""

    name: str
    enabled: bool


class OwnerUser(BaseModel):
    sub: str
    email: str


class PromptOwner(BaseModel):
    """Who holds this prompt, and whether it can still reject anything.

    `state` is resolved, shared or unknown: a prompt that was edited or
    deleted resolves to nobody, and 29% of rejections measured over the
    corpus are in that state. `enabled` is null when no current filter
    carries this prompt at all, because then there is nothing to ask.
    """

    state: str
    user_count: int
    users: list[OwnerUser]
    enabled: bool | None
    filters: list[OwnedFilter]


class PromptVersion(BaseModel):
    """One prompt version, which is the unit here rather than one filter.

    `sibling_hashes_by_name` is per name, not a single number: a hash can
    carry several names, and a scalar would leave a caller unable to say
    which name it describes.
    """

    prompt_hash: str
    filter_names: list[str]
    sibling_hashes_by_name: dict[str, int]
    first_seen: datetime.datetime | None
    last_seen: datetime.datetime | None
    owner: PromptOwner
    sufficient: bool
    totals: PromptTotals
    groups: list[GroupBreakdown]
    ungrouped: Breakdown


class MyPromptVersion(BaseModel):
    """The same prompt version as its owner sees it: their own filter names
    rather than every name the prompt has been given, and no owner block,
    because they are it."""

    prompt_hash: str
    filters: list[OwnedFilter]
    first_seen: datetime.datetime | None
    last_seen: datetime.datetime | None
    sufficient: bool
    totals: PromptTotals
    groups: list[GroupBreakdown]
    ungrouped: Breakdown


class ReasonsEnvelope(BaseModel):
    """What both rejection-reason views say about how they counted, so the
    two surfaces cannot describe the same prompt differently."""

    window_days: int
    unit: str = "decisions"
    min_decisions: int
    overlapping_groups: bool = True
    examples_selection: ExamplesSelection
    evidence_missing_criterion: EvidenceCriterion


class RejectionReasons(ReasonsEnvelope):
    prompt_versions: list[PromptVersion]
    filters: dict[str, list[str]]
    filterable: list[str]


class MyRejectionReasons(ReasonsEnvelope):
    prompt_versions: list[MyPromptVersion]


def _criterion() -> EvidenceCriterion:
    return EvidenceCriterion(
        method="phrase_match",
        description=EVIDENCE_MISSING_DESCRIPTION,
        phrases=list(EVIDENCE_MISSING_PHRASES),
    )


def _examples_selection() -> ExamplesSelection:
    """How the examples were chosen, published for the same reason the
    evidence-missing criterion is: a caller that has to narrate a number will
    narrate it from somewhere, and if the derivation is not in the payload it
    comes from memory, or from whoever explained it once. That is not
    hypothetical - `distinct_phrasings` counts exactly what its name says, and
    a page still rendered "the three most common" over it, because the field
    was named correctly and the sentence around it was not.
    """
    return ExamplesSelection(
        method="sample",
        ordered_by="decisions desc, then phrasing",
        description=(
            "A reproducible sample of `distinct_phrasings`, not a frequency "
            "ranking. Phrasings in this corpus almost never repeat, so counts "
            "are nearly all 1 and there is no meaningful 'most common'."
        ),
    )


def _bucket() -> dict[str, Any]:
    return {
        "decisions": 0,
        "distinct_jobs": set(),
        "phrasings": collections.Counter(),
        "evidence_missing_decisions": 0,
        "evidence_missing_jobs": set(),
    }


def _classify_rejections(
    hashes: list[str], rejections: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """One classification pass, shared by the admin and user views.

    A reason can land in several groups, so the per-hash evidence-missing
    totals ride along here rather than being summed from the groups
    afterwards: a reason in three groups is still one decision.
    """
    grouped: dict[str, dict[str, dict[str, Any]]] = {
        h: collections.defaultdict(_bucket) for h in hashes
    }
    per_hash: dict[str, dict[str, Any]] = {h: {"decisions": 0, "jobs": set()} for h in hashes}
    for row in rejections:
        buckets = grouped[row["prompt_hash"]]
        reason, url = row["reason"], row["url"]
        missing = is_evidence_missing(reason)
        if missing:
            totals_for_hash = per_hash[row["prompt_hash"]]
            totals_for_hash["decisions"] += 1
            totals_for_hash["jobs"].add(url)
        keys = classify(reason) or (UNGROUPED,)
        for key in keys:
            b = buckets[key]
            b["decisions"] += 1
            b["distinct_jobs"].add(url)
            b["phrasings"][reason] += 1
            if missing:
                b["evidence_missing_decisions"] += 1
                b["evidence_missing_jobs"].add(url)
    return grouped, per_hash


def _finish(bucket: dict[str, Any], examples: int) -> Breakdown:
    return Breakdown(
        decisions=bucket["decisions"],
        distinct_jobs=len(bucket["distinct_jobs"]),
        distinct_phrasings=len(bucket["phrasings"]),
        evidence_missing_decisions=bucket["evidence_missing_decisions"],
        evidence_missing_distinct_jobs=len(bucket["evidence_missing_jobs"]),
        # Most frequent phrasings rather than arbitrary ones: an example is
        # standing in for a group, so it should be typical of it.
        # Ordered by count then text, which makes the choice reproducible
        # rather than dependent on the order rows arrived in. It does NOT make
        # it representative: the model writes a near-unique sentence almost
        # every time - 30 of 14,050 distinct phrasings in production repeat at
        # all - so nearly every count here is 1 and "most frequent" is not a
        # meaningful ranking. Treat these as a sample of `distinct_phrasings`,
        # which is why that number ships beside them, and use the phrasings
        # listing when the distribution itself is the question.
        examples=[
            text
            for text, _ in sorted(bucket["phrasings"].items(), key=lambda kv: (-kv[1], kv[0]))[
                :examples
            ]
        ],
    )


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
        evidence_missing_criterion=_criterion(),
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
        _WINDOW,
    ]
    params: dict[str, Any] = {"prompt_hash": prompt_hash, "days": days}
    ids = scoping.user_ids(users)
    if ids:
        where.append(scoping.filters_of())
        params["user_ids"] = ids
    if group is not None:
        if group == UNGROUPED:
            # The residual is defined by matching nothing, so it is the
            # conjunction of every pattern's negation rather than a pattern.
            for g in GROUPS:
                where.append(f"reason !~* %(ng_{g.key})s")
                params[f"ng_{g.key}"] = reason_taxonomy.sql_pattern(g.key)
        else:
            where.append("reason ~* %(group)s")
            params["group"] = reason_taxonomy.sql_pattern(group)
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
    min_decisions: int = Query(_DEFAULT_MIN_DECISIONS, ge=1),
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
               max(created_at) AS last_seen,
               array_remove(array_agg(DISTINCT filter_name), NULL) AS filter_names
        FROM ai_queries
        WHERE check_type = 'custom' AND prompt_hash IS NOT NULL
          AND {_WINDOW}{hash_clause}
        GROUP BY 1
        """,
        params,
    )
    if not totals:
        return RejectionReasons(
            window_days=days,
            min_decisions=min_decisions,
            examples_selection=_examples_selection(),
            evidence_missing_criterion=_criterion(),
            prompt_versions=[],
            filters=applied,
            filterable=filterable,
        )

    rejections = db.query(
        f"""
        SELECT prompt_hash, url, reason FROM ai_queries
        WHERE check_type = 'custom' AND status = 'rejected'
          AND prompt_hash IS NOT NULL AND reason IS NOT NULL AND reason <> ''
          AND {_WINDOW}{hash_clause}
        """,
        params,
    )

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

    grouped, per_hash = _classify_rejections(hashes, rejections)

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
                totals=_totals(row, missing),
                groups=_groups(buckets, examples),
                ungrouped=_finish(buckets.get(UNGROUPED) or _bucket(), examples),
            )
        )

    out.sort(key=lambda r: r.totals.rejected, reverse=True)
    return RejectionReasons(
        window_days=days,
        min_decisions=min_decisions,
        examples_selection=_examples_selection(),
        evidence_missing_criterion=_criterion(),
        prompt_versions=out,
        filters=applied,
        filterable=filterable,
    )


def _totals(row: dict[str, Any], missing: dict[str, Any]) -> PromptTotals:
    return PromptTotals(
        evaluated=row["evaluated"],
        passed=row["passed"],
        rejected=row["rejected"],
        rejected_with_reason=row["rejected_with_reason"],
        distinct_jobs_evaluated=row["distinct_jobs_evaluated"],
        distinct_jobs_rejected=row["distinct_jobs_rejected"],
        evidence_missing_decisions=missing["decisions"],
        evidence_missing_distinct_jobs=len(missing["jobs"]),
    )


def _groups(buckets: dict[str, dict[str, Any]], examples: int) -> list[GroupBreakdown]:
    return sorted(
        (
            GroupBreakdown(
                **_finish(buckets[g.key], examples).model_dump(),
                key=g.key,
                label=GROUP_LABELS[g.key],
            )
            for g in GROUPS
            if g.key in buckets
        ),
        key=lambda g: g.decisions,
        reverse=True,
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
    min_decisions: int = Query(_DEFAULT_MIN_DECISIONS, ge=1),
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
            examples_selection=_examples_selection(),
            evidence_missing_criterion=_criterion(),
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
        WHERE check_type = 'custom' AND prompt_hash = ANY(%(hashes)s) AND {_WINDOW}
        GROUP BY 1
        """,
        params,
    )
    rejections = db.query(
        f"""
        SELECT prompt_hash, url, reason FROM ai_queries
        WHERE check_type = 'custom' AND status = 'rejected'
          AND prompt_hash = ANY(%(hashes)s)
          AND reason IS NOT NULL AND reason <> '' AND {_WINDOW}
        """,
        params,
    )
    grouped, per_hash = _classify_rejections(hashes, rejections)

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
                totals=_totals(row, missing),
                groups=_groups(buckets, examples),
                ungrouped=_finish(buckets.get(UNGROUPED) or _bucket(), examples),
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
                    ungrouped=_finish(_bucket(), examples),
                )
            )
    out.sort(key=lambda r: r.totals.rejected, reverse=True)
    return MyRejectionReasons(
        window_days=days,
        min_decisions=min_decisions,
        examples_selection=_examples_selection(),
        evidence_missing_criterion=_criterion(),
        prompt_versions=out,
    )
