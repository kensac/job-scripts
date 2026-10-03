"""Durable admission evidence, separate from paid verdicts and disposable tasks."""

from __future__ import annotations

import hashlib
from typing import Any

from api import db, review_decision_storage, review_policy_storage
from core.filters import build_custom_input
from core.job_profile import CLASSIFIER_VERSION, JOB_PROFILE_INSTRUCTIONS, JOB_PROFILE_MODEL


def content_hash(content: str | None) -> str | None:
    return hashlib.sha256(content.encode()).hexdigest() if content is not None else None


def decision(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": row["id"],
        "stage": row["stage"],
        "skip": row["action"] == "skip",
        "reason": row["reason"],
        "profile_id": row["profile_id"],
        "routing": row["evidence"].get("routing"),
    }


def existing(task_id: int) -> dict[str, dict[str, Any]]:
    return {
        row["url"]: review_decision_storage.resolve(row)
        for row in db.query(
            "SELECT d.id,d.url,d.prompt_hash,d.title,d.content_hash,d.evidence,d.stage,d.action,"
            f"d.reason,d.profile_id,{review_decision_storage.RESOLVED_COLUMNS} "
            f"FROM {review_decision_storage.RESOLVED_FROM} WHERE d.task_id=%s",
            (task_id,),
        )
    }


def validate_input(
    old: dict[str, Any],
    job: dict[str, Any],
    prompt_hash: str,
    contents: dict[str, str] | None,
    model: str | None,
    transport: str | None,
) -> None:
    evidence = old["evidence"]
    current_content = contents.get(job["url"]) if contents is not None else None
    current_input_hash = (
        content_hash(
            build_custom_input(job.get("company") or "", job.get("title") or "", current_content)
        )
        if current_content is not None
        else None
    )
    if (
        old["prompt_hash"] != prompt_hash
        or old["title"] != (job.get("title") or "")
        or evidence.get("company") != job.get("company")
        or (contents is not None and old["content_hash"] != content_hash(current_content))
        or (contents is not None and evidence.get("input_hash") != current_input_hash)
        or (
            model is not None
            and evidence.get("planned_model") is not None
            and evidence["planned_model"] != model
        )
        or (
            transport is not None
            and evidence.get("transport") is not None
            and evidence["transport"] != transport
        )
    ):
        raise RuntimeError("Review gate input changed within an immutable run")


def persist(
    task_id: int,
    prompt_hash: str,
    jobs: list[dict[str, Any]],
    contents: dict[str, str] | None,
    decisions: dict[str, dict[str, Any]],
    policy: dict[str, Any],
    profiles: dict[str, Any],
    *,
    model: str | None,
    transport: str | None,
    observations: dict[str, Any],
    filter_id: int | None,
) -> dict[str, dict[str, Any]]:
    task = db.query_one("SELECT payload FROM tasks WHERE id=%s FOR UPDATE", (task_id,))
    if task is None:
        raise RuntimeError("Review gate task disappeared before admission")
    payload = task["payload"]
    previous = existing(task_id)
    if any(row["prompt_hash"] != prompt_hash for row in previous.values()):
        raise RuntimeError("Review gate prompt changed within an immutable run")
    identities = {
        row["url"]: row["id"]
        for row in db.query(
            "SELECT id,url FROM jobs WHERE url=ANY(%s::text[])", ([job["url"] for job in jobs],)
        )
    }
    rows: list[tuple[Any, ...]] = []
    bodies: list[dict[str, Any]] = []
    for job in jobs:
        url = job["url"]
        if url in previous:
            validate_input(previous[url], job, prompt_hash, contents, model, transport)
            continue
        selected = decisions.get(
            url, {"stage": "detailed", "skip": False, "reason": None, "profile_id": None}
        )
        profile = profiles.get(url)
        rows.append(
            (
                task_id,
                url,
                job.get("id") or identities.get(url),
                payload.get("user_id")
                if payload.get("user_id") is not None
                else payload.get("sponsor_user_id"),
                filter_id if filter_id is not None else payload.get("filter_id"),
                payload.get("managed_board_id"),
                payload.get("revision"),
            )
        )
        bodies.append(
            {
                "key": len(bodies),
                "prompt_hash": prompt_hash,
                "stage": selected["stage"],
                "mode": policy.get(selected["stage"] + "_mode", "off"),
                "action": "skip" if selected["skip"] else "review",
                "reason": selected["reason"],
                "profile_id": selected["profile_id"],
                "title": job.get("title") or "",
                "content_hash": content_hash((contents or {}).get(url)),
                "evidence": {
                    "version": "review-gate-v1",
                    "company": job.get("company"),
                    "planned_model": model,
                    "transport": transport,
                    "routing": observations.get(url),
                    "input_hash": content_hash(
                        build_custom_input(
                            job.get("company") or "", job.get("title") or "", contents[url]
                        )
                    )
                    if contents and url in contents
                    else None,
                    "profile": profile[1].model_dump(mode="json") if profile else None,
                    "profile_classifier_version": CLASSIFIER_VERSION if profile else None,
                    "profile_model": JOB_PROFILE_MODEL if profile else None,
                    "profile_instructions": JOB_PROFILE_INSTRUCTIONS if profile else None,
                    "profile_input_content": (contents or {}).get(url) if profile else None,
                },
            }
        )
    if not rows:
        return previous
    policy_id = review_policy_storage.intern(db.jsonb(policy))
    for body in bodies:
        body["policy_id"] = policy_id
    urls = review_decision_storage.intern_urls([row[1] for row in rows])
    body_ids = review_decision_storage.intern_bodies(
        "SELECT * FROM jsonb_to_recordset(%(bodies)s::jsonb) AS s(key int,prompt_hash text,"
        "stage text,mode text,action text,reason text,profile_id bigint,title text,"
        "content_hash text,policy_id bigint,evidence jsonb)",
        {"bodies": db.jsonb(bodies)},
    )
    # Reference-only: the content lives once in the body and URL tables.
    db.executemany(
        "INSERT INTO review_gate_decisions(task_id,url_id,job_id,user_id,filter_id,"
        "managed_board_id,revision,body_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT(task_id,url_id) DO NOTHING",
        [(row[0], urls[row[1]], *row[2:], body_ids[n]) for n, row in enumerate(rows)],
    )
    return existing(task_id)


def record_outcome(decision_id: int | None, query_id: int | None) -> None:
    if decision_id is None or query_id is None:
        return
    db.execute(
        "INSERT INTO review_gate_outcomes(decision_id,query_id,batch_id,model,rejected,outcome,"
        "recorded_cost_usd,usage) SELECT %s,q.id,q.batch_id,q.model,"
        "CASE WHEN q.status='passed' THEN false WHEN q.status='rejected' THEN true END,"
        "CASE WHEN q.status IN ('passed','rejected') THEN 'written' ELSE 'failed' END,"
        "q.cost_usd,jsonb_build_object('prompt_tokens',q.prompt_tokens,"
        "'completion_tokens',q.completion_tokens,'cached_tokens',q.cached_tokens,"
        "'cache_write_tokens',q.cache_write_tokens,'reasoning_tokens',q.reasoning_tokens) "
        "FROM ai_queries q WHERE q.id=%s ON CONFLICT(decision_id,query_id) DO NOTHING",
        (decision_id, query_id),
    )


def exclusions(task_id: int, prompt_hash: str) -> set[str] | None:
    rows = db.query(
        f"SELECT d.url,d.prompt_hash,d.action FROM {review_decision_storage.DECISIONS} "
        "WHERE d.task_id=%s",
        (task_id,),
    )
    if not rows:
        return None
    if any(row["prompt_hash"] != prompt_hash for row in rows):
        raise RuntimeError("Review gate prompt changed within an immutable run")
    return {row["url"] for row in rows if row["action"] == "skip"}
