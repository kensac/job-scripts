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
from typing import Any

from fastapi import APIRouter, Depends, Query

from api import db
from api.auth import AuthedUser
from api.routers.admin import require_admin
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


def _bucket() -> dict[str, Any]:
    return {
        "decisions": 0,
        "distinct_jobs": set(),
        "phrasings": collections.Counter(),
        "evidence_missing_decisions": 0,
        "evidence_missing_jobs": set(),
    }


def _finish(bucket: dict[str, Any], examples: int) -> dict[str, Any]:
    return {
        "decisions": bucket["decisions"],
        "distinct_jobs": len(bucket["distinct_jobs"]),
        "distinct_phrasings": len(bucket["phrasings"]),
        "evidence_missing_decisions": bucket["evidence_missing_decisions"],
        "evidence_missing_distinct_jobs": len(bucket["evidence_missing_jobs"]),
        # Most frequent phrasings rather than arbitrary ones: an example is
        # standing in for a group, so it should be typical of it.
        "examples": [text for text, _ in bucket["phrasings"].most_common(examples)],
    }


@router.get("/rejection-reasons")
def rejection_reasons(
    days: int = Query(30, ge=1, le=365),
    prompt_hash: str | None = None,
    min_decisions: int = Query(_DEFAULT_MIN_DECISIONS, ge=1),
    examples: int = Query(3, ge=0, le=5),
    user: AuthedUser = Depends(require_admin),
) -> dict[str, Any]:
    params: dict[str, Any] = {"days": days}
    hash_clause = ""
    if prompt_hash:
        hash_clause = " AND prompt_hash = %(prompt_hash)s"
        params["prompt_hash"] = prompt_hash

    totals = db.query(
        f"""
        SELECT prompt_hash,
               count(*) AS evaluated,
               count(*) FILTER (WHERE status = 'passed') AS passed,
               count(*) FILTER (WHERE status = 'rejected') AS rejected,
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
        return _empty(days, min_decisions)

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
    owners: dict[str, dict[str, dict[str, str]]] = collections.defaultdict(dict)
    filters_for: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in db.query(
        """
        SELECT f.prompt_hash, f.name, f.enabled, u.sub, u.email FROM user_filters f
        JOIN users u ON u.id = f.user_id WHERE f.prompt_hash = ANY(%s)
        ORDER BY f.name
        """,
        (hashes,),
    ):
        owners[row["prompt_hash"]][row["sub"]] = {
            "sub": row["sub"],
            "email": row["email"] or "",
        }
        filters_for[row["prompt_hash"]].append({"name": row["name"], "enabled": row["enabled"]})

    # A name that appears under more than one hash in this window spans a
    # prompt edit; the caller needs to know before it presents them as one row.
    by_name: dict[str, set[str]] = collections.defaultdict(set)
    for row in totals:
        for name in row["filter_names"]:
            by_name[name].add(row["prompt_hash"])

    grouped: dict[str, dict[str, dict[str, Any]]] = {
        h: collections.defaultdict(_bucket) for h in hashes
    }
    # Per-hash evidence-missing totals ride along on the same pass; they are
    # not the sum of the groups', because a reason in three groups is still
    # one decision.
    per_hash: dict[str, dict[str, Any]] = {h: {"decisions": 0, "jobs": set()} for h in hashes}
    for row in rejections:
        buckets = grouped[row["prompt_hash"]]
        reason, url = row["reason"], row["url"]
        missing = is_evidence_missing(reason)
        if missing:
            totals_for_hash = per_hash[row["prompt_hash"]]
            totals_for_hash["decisions"] += 1
            totals_for_hash["jobs"].add(url)
        keys = classify(reason) or ("__ungrouped__",)
        for key in keys:
            b = buckets[key]
            b["decisions"] += 1
            b["distinct_jobs"].add(url)
            b["phrasings"][reason] += 1
            if missing:
                b["evidence_missing_decisions"] += 1
                b["evidence_missing_jobs"].add(url)

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
            {
                "prompt_hash": h,
                "filter_names": sorted(row["filter_names"]),
                "sibling_hashes_by_name": siblings_by_name,
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "owner": {
                    "state": "resolved"
                    if len(people) == 1
                    else ("shared" if len(people) > 1 else "unknown"),
                    "user_count": len(people),
                    "users": people,
                    # Whether this prompt can still reject anything. `resolved`
                    # does not answer it: a disabled filter is still a current
                    # row, so a retired prompt resolves to its owner exactly
                    # like a live one. Without this a caller ranking by misfire
                    # rate leads with a filter that cannot fire, spending the
                    # reader's attention on the one thing they cannot act on.
                    # None when no current filter carries this prompt at all,
                    # because then there is nothing to ask.
                    "enabled": any(f["enabled"] for f in current) if current else None,
                    "filters": current,
                },
                "sufficient": row["rejected"] >= min_decisions,
                "totals": {
                    "evaluated": row["evaluated"],
                    "passed": row["passed"],
                    "rejected": row["rejected"],
                    "distinct_jobs_evaluated": row["distinct_jobs_evaluated"],
                    "distinct_jobs_rejected": row["distinct_jobs_rejected"],
                    "evidence_missing_decisions": missing["decisions"],
                    "evidence_missing_distinct_jobs": len(missing["jobs"]),
                },
                "groups": sorted(
                    (
                        {
                            "key": g.key,
                            "label": GROUP_LABELS[g.key],
                            **_finish(buckets[g.key], examples),
                        }
                        for g in GROUPS
                        if g.key in buckets
                    ),
                    key=lambda g: g["decisions"],
                    reverse=True,
                ),
                "ungrouped": _finish(buckets.get("__ungrouped__") or _bucket(), examples),
            }
        )

    out.sort(key=lambda r: r["totals"]["rejected"], reverse=True)
    return {
        "window_days": days,
        "unit": "decisions",
        "min_decisions": min_decisions,
        "overlapping_groups": True,
        "evidence_missing_criterion": {
            "method": "phrase_match",
            "description": EVIDENCE_MISSING_DESCRIPTION,
            "phrases": list(EVIDENCE_MISSING_PHRASES),
        },
        "prompt_versions": out,
    }


def _empty(days: int, min_decisions: int) -> dict[str, Any]:
    return {
        "window_days": days,
        "unit": "decisions",
        "min_decisions": min_decisions,
        "overlapping_groups": True,
        "evidence_missing_criterion": {
            "method": "phrase_match",
            "description": EVIDENCE_MISSING_DESCRIPTION,
            "phrases": list(EVIDENCE_MISSING_PHRASES),
        },
        "prompt_versions": [],
    }
