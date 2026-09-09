from __future__ import annotations

import datetime
import logging

from api import batch_results, budget, db

logger = logging.getLogger("jobtracker_worker")
PURPOSE = "application"


def reserve_task(task_id: int, user_id: int, rows: list[dict] | None = None) -> dict[str, dict]:
    """Persist answer generations before any provider submission.

    A manual task reserves at enqueue time, so its intent supersedes parked
    automatic work before a worker starts it. Replays reuse the reservation.
    """
    with db.transaction():
        task = db.query_one("SELECT kind, payload FROM tasks WHERE id = %s FOR UPDATE", (task_id,))
        if task is None:
            raise LookupError("draft task no longer exists")
        payload = task["payload"]
        requests = dict(payload.get("draft_requests") or {})
        if rows is None:
            rows = db.query(
                "SELECT job_id, key FROM application_answers WHERE user_id = %s AND job_id = %s "
                "AND (%s::text[] IS NULL OR key = ANY(%s)) ORDER BY id",
                (
                    user_id,
                    payload["job_id"],
                    payload.get("keys") or None,
                    payload.get("keys") or None,
                ),
            )
        wanted = {(r["job_id"], r["key"]) for r in rows}
        answers = db.query(
            "SELECT id, job_id, key, question, draft, turns, draft_revision FROM application_answers "
            "WHERE user_id = %s AND job_id = ANY(%s) ORDER BY id FOR UPDATE",
            (user_id, sorted({job_id for job_id, _ in wanted})),
        )
        selected = {}
        for answer in answers:
            if (answer["job_id"], answer["key"]) not in wanted:
                continue
            custom_id = f"{answer['job_id']}|{answer['key']}"
            if custom_id in (payload.get("draft_results") or {}):
                continue
            if custom_id in requests:
                saved = requests[custom_id]
                if (
                    saved["answer_id"] == answer["id"]
                    and saved["revision"] == answer["draft_revision"]
                ):
                    selected[custom_id] = {**saved, "question": answer["question"]}
                continue
            if task["kind"] == "application_sweep":
                # An explicit clear is still an edit. Null alone cannot tell
                # it apart from an answer nobody has drafted or touched.
                if answer["draft"] is not None or answer["turns"]:
                    continue
                if db.query_one(
                    "SELECT 1 FROM tasks WHERE kind = 'application_draft' "
                    "AND status IN ('pending','running','awaiting_batch','waiting') "
                    "AND (payload->>'user_id')::bigint = %s "
                    "AND (payload->>'job_id')::bigint = %s LIMIT 1",
                    (user_id, answer["job_id"]),
                ):
                    continue
            revision = answer["draft_revision"] + 1
            db.execute(
                "UPDATE application_answers SET draft_revision = %s WHERE id = %s",
                (revision, answer["id"]),
            )
            requests[custom_id] = {"answer_id": answer["id"], "revision": revision}
            selected[custom_id] = {**requests[custom_id], "question": answer["question"]}
        db.execute(
            "UPDATE tasks SET payload = jsonb_set(payload, '{draft_requests}', %s) WHERE id = %s",
            (db.jsonb(requests), task_id),
        )
        return selected


def apply_result(
    user_id: int,
    request: dict | None,
    answer: str | None,
    usage: dict[str, int],
    key_source: str,
    model: str | None,
    kind: str,
    *,
    batched: bool,
) -> str:
    """Book consumed usage and apply only the reserved answer generation.

    The consumer's transaction must also acknowledge its result identity.
    This nested transaction participates in that same database connection.
    """
    with db.transaction():
        budget.record_tokens(user_id, key_source, PURPOSE, model, usage, batched=batched)
        if answer is None:
            return "failed"
        if not request or "answer_id" not in request or "revision" not in request:
            return "unknown_request"
        turn = {
            "role": "assistant",
            "kind": kind,
            "text": answer,
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        written = db.execute_count(
            "UPDATE application_answers SET draft = %s, model = %s, turns = turns || %s::jsonb, "
            "updated_at = now(), draft_revision = draft_revision + 1 "
            "WHERE id = %s AND user_id = %s AND draft_revision = %s",
            (answer, model, db.jsonb([turn]), request["answer_id"], user_id, request["revision"]),
        )
        return "written" if written else "superseded"


def record_result(
    task_id: int,
    user_id: int,
    custom_id: str,
    answer: str | None,
    usage: dict[str, int],
    key_source: str,
    model: str | None,
    kind: str,
    *,
    batched: bool,
) -> int:
    with db.transaction():
        task = db.query_one("SELECT payload FROM tasks WHERE id = %s FOR UPDATE", (task_id,))
        if task is None:
            raise LookupError("draft task no longer exists")
        payload = task["payload"]
        recorded = dict(payload.get("draft_results") or {})
        if custom_id in recorded:
            return 0
        request = (payload.get("draft_requests") or {}).get(custom_id)
        outcome = apply_result(
            user_id, request, answer, usage, key_source, model, kind, batched=batched
        )
        recorded[custom_id] = outcome
        db.execute(
            "UPDATE tasks SET payload = jsonb_set(payload, '{draft_results}', %s) WHERE id = %s",
            (db.jsonb(recorded), task_id),
        )
    if outcome in {"unknown_request", "superseded"}:
        logger.info("Draft task %s result %s was not applied: %s", task_id, custom_id, outcome)
    return int(outcome == "written")


def outcome_note(task_id: int) -> str:
    task = db.query_one(
        "SELECT payload->'draft_results' AS results FROM tasks WHERE id = %s", (task_id,)
    )
    results = (task or {}).get("results") or {}
    counts = {}
    for value in results.values():
        counts[value] = counts.get(value, 0) + 1
    for outcome, count in batch_results.outcome_counts(task_id).items():
        counts[outcome] = counts.get(outcome, 0) + count
    if not counts:
        return ""
    return "; " + ", ".join(
        f"{count} {outcome.replace('_', ' ')}" for outcome, count in sorted(counts.items())
    )


def progress_counts(task_id: int, minimum_total: int) -> tuple[int, int]:
    done, total = batch_results.progress_counts(task_id)
    task = db.query_one("SELECT payload FROM tasks WHERE id = %s", (task_id,))
    payload = (task or {}).get("payload") or {}
    live = payload.get("draft_results") or {}
    done += sum(outcome == "written" for outcome in live.values())
    return done, max(minimum_total, total + len(live), len(payload.get("draft_requests") or {}))
