from __future__ import annotations

import datetime
import logging

from api import budget, db

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
            "SELECT id, job_id, key, draft, turns, draft_revision FROM application_answers "
            "WHERE user_id = %s AND job_id = ANY(%s) ORDER BY id FOR UPDATE",
            (user_id, sorted({job_id for job_id, _ in wanted})),
        )
        selected = {}
        for answer in answers:
            if (answer["job_id"], answer["key"]) not in wanted:
                continue
            custom_id = f"{answer['job_id']}|{answer['key']}"
            if custom_id in requests:
                saved = requests[custom_id]
                if (
                    saved["answer_id"] == answer["id"]
                    and saved["revision"] == answer["draft_revision"]
                ):
                    selected[custom_id] = saved
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
            selected[custom_id] = requests[custom_id] = {
                "answer_id": answer["id"],
                "revision": revision,
            }
        db.execute(
            "UPDATE tasks SET payload = jsonb_set(payload, '{draft_requests}', %s) WHERE id = %s",
            (db.jsonb(requests), task_id),
        )
        return selected


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
        budget.record_tokens(user_id, key_source, PURPOSE, model, usage, batched=batched)
        request = (payload.get("draft_requests") or {}).get(custom_id)
        outcome = (
            "failed" if answer is None else "unknown_request" if request is None else "superseded"
        )
        written = 0
        if answer is not None and request is not None:
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
                (
                    answer,
                    model,
                    db.jsonb([turn]),
                    request["answer_id"],
                    user_id,
                    request["revision"],
                ),
            )
            if written:
                outcome = "written"
        recorded[custom_id] = outcome
        db.execute(
            "UPDATE tasks SET payload = jsonb_set(payload, '{draft_results}', %s) WHERE id = %s",
            (db.jsonb(recorded), task_id),
        )
    if outcome in {"unknown_request", "superseded"}:
        logger.info("Draft task %s result %s was not applied: %s", task_id, custom_id, outcome)
    return written


def outcome_note(task_id: int) -> str:
    task = db.query_one(
        "SELECT payload->'draft_results' AS results FROM tasks WHERE id = %s", (task_id,)
    )
    results = (task or {}).get("results") or {}
    if not results:
        return ""
    counts = {}
    for value in results.values():
        counts[value] = counts.get(value, 0) + 1
    return "; " + ", ".join(
        f"{count} {outcome.replace('_', ' ')}" for outcome, count in sorted(counts.items())
    )
