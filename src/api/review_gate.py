"""Reversible pre-submission exclusions, never synthetic paid verdicts."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any

from api import db, review_gate_records
from api.ai import batch_results, request_snapshots
from core.job_profile import (
    CLASSIFIER_VERSION,
    JOB_PROFILE_INSTRUCTIONS,
    JOB_PROFILE_MODEL,
    JobProfileAnswer,
    build_job_profile_input,
    job_profile_spec,
)
from core.payload_objects import (
    MAX_CONNECTIONS,
    BundleCache,
    PayloadStore,
    parse_ref,
)
from core.pool import in_transaction
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
    """`timeout_ms` bounds the whole lookup, database and object reads together;
    past it this raises, and admission retains detailed review."""
    if not contents:
        return {}
    deadline = time.monotonic() + timeout_ms / 1000
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
                "SELECT r.custom_id, r.response->>'text' AS answer, b.snapshot,b.snapshot_ref "
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

    def answered(receipt: dict[str, Any], answer: JobProfileAnswer) -> bool:
        return bool(
            receipt["answer"] and JobProfileAnswer.model_validate_json(receipt["answer"]) == answer
        )

    def proves(row: dict[str, Any], receipt: dict[str, Any], answer: JobProfileAnswer) -> bool:
        url = row["url"]
        spec = request_snapshots.resolve(receipt)
        if spec is None:
            return False
        snapshot = dataclasses.asdict(spec)
        context = snapshot.get("context") or {}
        return (
            snapshot.get("instructions") == JOB_PROFILE_INSTRUCTIONS
            and snapshot.get("input") == build_job_profile_input(titles[url], contents[url])
            and context.get("url") == url
            and context.get("content_row_id") == row["content_row_id"]
            and context.get("classifier_version") == CLASSIFIER_VERSION
            and context.get("content_hash") == row["content_hash"]
            and answered(receipt, answer)
        )

    result = {}
    unread: list[tuple[dict[str, Any], JobProfileAnswer, list[dict[str, Any]]]] = []
    for row in rows:
        url = row["url"]
        if url not in titles or not titles[url].strip():
            continue
        answer = JobProfileAnswer.model_validate(row)
        # A reference records the digest of the request it stands for. The
        # request this proof wants is rebuilt from the same recipe, so an
        # equal digest means the stored request is it byte for byte: every
        # comparison in proves() holds by construction and nothing is read.
        # A request that differs anywhere, including in fields the proof does
        # not compare, is read and compared as before.
        digest = batch_results.snapshot_sha256(
            job_profile_spec(
                url, row["content_row_id"], titles[url], contents[url], row["content_hash"]
            )
        )
        later = []
        for receipt in by_id.get(str(row["content_row_id"]), []):
            if receipt["snapshot"] is None and receipt["snapshot_ref"] is not None:
                if request_snapshots.digest_and_size(receipt["snapshot_ref"])[0] != digest:
                    later.append(receipt)
                    continue
                if answered(receipt, answer):
                    result[url] = (row["id"], answer)
                    break
                continue
            if proves(row, receipt, answer):
                result[url] = (row["id"], answer)
                break
        else:
            if later:
                unread.append((row, answer, later))
    if unread:
        _hydrate([receipt for _, _, later in unread for receipt in later], deadline)
        for row, answer, later in unread:
            if any(proves(row, receipt, answer) for receipt in later):
                result[row["url"]] = (row["id"], answer)
    return result


def _hydrate(receipts: list[dict[str, Any]], deadline: float) -> None:
    """Read each receipt's referenced request into it, or raise by the deadline.

    One reader per object, concurrently, each with its own BundleCache. The
    client's own timeouts are the remaining budget, so a read abandoned at
    the deadline ends by itself within that budget again.
    """
    if in_transaction():
        raise RuntimeError("Request hydration cannot run inside a database transaction")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Review gate profile lookup exceeded its budget")
    store = PayloadStore.from_env(timeout=remaining)
    objects: dict[str, list[dict[str, Any]]] = {}
    for receipt in receipts:
        objects.setdefault(parse_ref(receipt["snapshot_ref"]).key, []).append(receipt)

    def read(group: list[dict[str, Any]]) -> list[Any]:
        cache: BundleCache = {}
        return [request_snapshots.load(receipt, store, cache) for receipt in group]

    executor = ThreadPoolExecutor(max_workers=MAX_CONNECTIONS)
    try:
        futures = {executor.submit(read, group): group for group in objects.values()}
        done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        if pending:
            raise TimeoutError("Review gate profile lookup exceeded its budget")
        for future in done:
            for receipt, snapshot in zip(futures[future], future.result(), strict=True):
                receipt["snapshot"] = snapshot
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def partition(
    task_id: int,
    prompt_hash: str,
    jobs: list[dict[str, Any]],
    contents: dict[str, str] | None,
    *,
    model: str | None = None,
    transport: str | None = None,
    observe: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    filter_id: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    # An admission is a fact. Configuration edits only affect a new run.
    stored = review_gate_records.existing(task_id)
    if any(row["prompt_hash"] != prompt_hash for row in stored.values()):
        raise RuntimeError("Review gate prompt changed within an immutable run")
    for job in jobs:
        old = stored.get(job["url"])
        if old:
            review_gate_records.validate_input(old, job, prompt_hash, contents, model, transport)
    if stored and all(job["url"] in stored for job in jobs):
        decisions = {job["url"]: review_gate_records.decision(stored[job["url"]]) for job in jobs}
        return [job for job in jobs if not decisions[job["url"]]["skip"]], decisions
    policy = (
        ReviewGatePolicy.model_validate(next(iter(stored.values()))["policy"])
        if stored
        else load_policy()
    )
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
    observations = observe([job for job in jobs if job["url"] not in skipped]) if observe else {}
    report = {
        "version": "review-gate-v1",
        "prompt_hash": prompt_hash,
        "policy": policy.model_dump(mode="json"),
        "candidates": len(jobs),
        "profile_proven": sum(d["profile_id"] is not None for d in decisions.values()),
        "would_reject": dict(
            Counter(d["stage"] for d in decisions.values() if d["stage"] != "detailed")
        ),
        "detailed": len(jobs) - len(skipped),
    }
    # The skipped URLs are not copied here. persist records a decision row for
    # every job in the same transaction, so review_gate_records.exclusions
    # derives exactly this set; the copy was 53% of filter-chunk payload text
    # over 7 days (2026-10-03) and rode along on every later payload rewrite.
    # Payloads written before #638 recorded decisions keep theirs, and
    # managed_board_runs.replace_projection still reads them when no rows exist.
    # Failure to persist provenance aborts before any requests are submitted.
    # Replace, never increment: retries cannot inflate the funnel.
    with db.transaction():
        persisted = review_gate_records.persist(
            task_id,
            prompt_hash,
            jobs,
            contents,
            decisions,
            policy.model_dump(mode="json"),
            profiles,
            model=model,
            transport=transport,
            observations=observations,
            filter_id=filter_id,
        )
        decisions = {url: review_gate_records.decision(row) for url, row in persisted.items()}
        skipped = {url: value for url, value in decisions.items() if value["skip"]}
        report["policy"] = (
            next(iter(persisted.values()))["policy"] if persisted else report["policy"]
        )
        report["candidates"] = len(decisions)
        report["detailed"] = len(decisions) - len(skipped)
        report["profile_proven"] = sum(d["profile_id"] is not None for d in decisions.values())
        report["would_reject"] = dict(
            Counter(d["stage"] for d in decisions.values() if d["stage"] != "detailed")
        )
        db.execute(
            "UPDATE tasks SET payload=jsonb_set(payload,'{review_gate}',%s) WHERE id=%s",
            (db.jsonb(report), task_id),
        )
    return [job for job in jobs if job["url"] not in skipped], decisions
