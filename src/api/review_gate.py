"""Reversible pre-submission exclusions, never synthetic paid verdicts."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from api import db, review_gate_records
from core.job_profile import (
    CLASSIFIER_VERSION,
    JOB_PROFILE_INSTRUCTIONS,
    JOB_PROFILE_MODEL,
    JobProfileAnswer,
    build_job_profile_input,
)
from core.review_gate import ReviewGatePolicy, profile_rejection, title_rejection
from core.store import get_contents

logger = logging.getLogger(__name__)


def load_policy() -> ReviewGatePolicy:
    try:
        return ReviewGatePolicy.model_validate(db.get_config("filter_review_gate") or {})
    except Exception:
        logger.exception("Review gate configuration unavailable; retaining detailed review")
        return ReviewGatePolicy()


def proven_profiles(
    jobs: list[dict[str, Any]], contents: dict[str, str], timeout_ms: int
) -> dict[str, tuple[int, JobProfileAnswer]]:
    if not contents:
        return {}
    titles = {job["url"]: job.get("title") or "" for job in jobs}
    # Legacy profile rows lack a title snapshot. A retained, consumed request
    # must prove both the original input and the response used for reuse.
    with db.transaction():
        previous = db.query_one("SELECT current_setting('statement_timeout') AS value")
        assert previous is not None
        db.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))
        rows = db.query(
            "SELECT DISTINCT ON (p.url) p.* FROM job_profiles p "
            "JOIN ai_queries c ON c.id=p.content_row_id "
            "JOIN unnest(%s::text[], %s::text[]) AS i(url, content) "
            "ON i.url=p.url AND i.content=c.input_content "
            "WHERE p.classifier_version=%s AND p.model=%s ORDER BY p.url,p.id DESC",
            (list(contents), list(contents.values()), CLASSIFIER_VERSION, JOB_PROFILE_MODEL),
        )
        receipts = (
            db.query(
                "WITH requests AS MATERIALIZED (SELECT b.* FROM tasks t "
                "JOIN batch_requests b ON b.task_id=t.id WHERE t.kind='classify_job_profiles' "
                "AND b.custom_id=ANY(%s::text[])) "
                "SELECT r.custom_id, r.response->>'text' AS answer, b.snapshot "
                "FROM requests b JOIN batch_result_receipts r USING(task_id,custom_id) "
                "WHERE r.model=%s AND r.outcome='written'",
                ([str(row["content_row_id"]) for row in rows], JOB_PROFILE_MODEL),
            )
            if rows
            else []
        )
        db.execute("SELECT set_config('statement_timeout', %s, true)", (previous["value"],))
    by_id: dict[str, list[dict[str, Any]]] = {}
    for receipt in receipts:
        by_id.setdefault(receipt["custom_id"], []).append(receipt)
    result = {}
    for row in rows:
        url = row["url"]
        if url not in titles or not titles[url].strip():
            continue
        answer = JobProfileAnswer.model_validate(row)
        for receipt in by_id.get(str(row["content_row_id"]), []):
            snapshot = receipt["snapshot"] or {}
            context = snapshot.get("context") or {}
            if (
                snapshot.get("instructions") == JOB_PROFILE_INSTRUCTIONS
                and snapshot.get("input") == build_job_profile_input(titles[url], contents[url])
                and context.get("url") == url
                and context.get("content_row_id") == row["content_row_id"]
                and context.get("classifier_version") == CLASSIFIER_VERSION
                and context.get("content_hash") == row["content_hash"]
                and receipt["answer"]
                and JobProfileAnswer.model_validate_json(receipt["answer"]) == answer
            ):
                result[url] = (row["id"], answer)
                break
    return result


def partition(
    task_id: int,
    prompt_hash: str,
    jobs: list[dict[str, Any]],
    contents: dict[str, str] | None,
    *,
    model: str | None = None,
    transport: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    # An admission is a fact. Configuration edits only affect a new run.
    stored = review_gate_records.existing(task_id)
    for job in jobs:
        old = stored.get(job["url"])
        if old and (
            old["prompt_hash"] != prompt_hash
            or old["title"] != (job.get("title") or "")
            or (
                contents is not None
                and old["content_hash"]
                != review_gate_records.content_hash(contents.get(job["url"]))
            )
        ):
            raise RuntimeError("Review gate input changed within an immutable run")
    if stored and all(job["url"] in stored for job in jobs):
        decisions = {job["url"]: review_gate_records.decision(stored[job["url"]]) for job in jobs}
        return [job for job in jobs if not decisions[job["url"]]["skip"]], decisions
    policy = load_policy()
    scope = policy.scopes.get(prompt_hash)
    decisions: dict[str, dict[str, Any]] = {}
    profiles = {}
    if scope is not None:
        profiles = {}
        if policy.profile_mode != "off" and scope.profile_recipe:
            try:
                if contents is None:
                    contents = get_contents([job["url"] for job in jobs if "content" not in job])
                    contents.update(
                        {job["url"]: job["content"] for job in jobs if job.get("content")}
                    )
                profiles = proven_profiles(jobs, contents, policy.lookup_timeout_ms)
            except Exception:
                logger.exception("Review gate profile evidence unavailable; retaining review")
        for job in jobs:
            url = job["url"]
            title_reason = (
                title_rejection(job.get("title") or "")
                if policy.title_mode != "off" and scope.title_recipe
                else None
            )
            evidence = profiles.get(url)
            profile_reason = (
                profile_rejection(evidence[1], job.get("title") or "") if evidence else None
            )
            # Shadow proposals cannot mask an independently enforced stage.
            stage = "title" if title_reason else "profile" if profile_reason else "detailed"
            skip = False
            if title_reason and policy.title_mode == "enforce":
                stage, skip = "title", True
            elif profile_reason and policy.profile_mode == "enforce":
                stage, skip = "profile", True
            decisions[url] = {
                "stage": stage,
                "skip": skip,
                "reason": title_reason if stage == "title" else profile_reason,
                "profile_id": evidence[0] if evidence else None,
            }
    skipped = {url: decision for url, decision in decisions.items() if decision["skip"]}
    report = {
        "version": "review-gate-v1",
        "prompt_hash": prompt_hash,
        "policy": policy.model_dump(mode="json"),
        "candidates": len(jobs),
        "profile_proven": sum(d["profile_id"] is not None for d in decisions.values()),
        "would_reject": dict(
            Counter(d["stage"] for d in decisions.values() if d["stage"] != "detailed")
        ),
        "skipped": skipped,
        "detailed": len(jobs) - len(skipped),
    }
    # Failure to persist provenance aborts before any requests are submitted.
    # Replace, never increment: retries cannot inflate the funnel.
    with db.transaction():
        decisions = review_gate_records.persist(
            task_id,
            prompt_hash,
            jobs,
            contents,
            decisions,
            policy.model_dump(mode="json"),
            profiles,
            model=model,
            transport=transport,
        )
        skipped = {url: value for url, value in decisions.items() if value["skip"]}
        report["skipped"] = skipped
        db.execute(
            "UPDATE tasks SET payload=jsonb_set(payload,'{review_gate}',%s) WHERE id=%s",
            (db.jsonb(report), task_id),
        )
    return [job for job in jobs if job["url"] not in skipped], decisions


def record_comparison(task_id: int, decision: dict[str, Any] | None, rejected: bool | None) -> None:
    if not decision or decision.get("stage") == "detailed":
        return
    key = "unresolved" if rejected is None else "agreed_reject" if rejected else "false_reject"
    # Called in the paid receipt transaction, so replay counts once.
    try:
        with db.transaction():
            db.execute(
                "UPDATE tasks SET payload=jsonb_set(payload,'{review_gate_comparison}', "
                "COALESCE(payload->'review_gate_comparison','{}'::jsonb) || "
                "jsonb_build_object(%s::text,COALESCE((payload->'review_gate_comparison'->>%s)::int,0)+1)) "
                "WHERE id=%s",
                (key, key, task_id),
            )
    except Exception:
        logger.exception("Review gate comparison unavailable")
