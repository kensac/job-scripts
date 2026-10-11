"""Classify stored mail and append the current event decisions.

Automatic scheduling is controlled by the persisted classification switch.
This handler also serves explicit repair tasks and paid batch resumption.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from api import db
from api.ai.batch_results import progress_counts
from api.mail import events as mail_events
from api.task_config import configured_shape
from core.shapes import BACKFILL_TASK, ONGOING_TASK
from tasks.mail.classification.dates import is_appointment, parse_when
from tasks.mail.classification.identity import _SELF_SENT, _heal_self_sent, identities_for
from tasks.mail.classification.spec import (
    CLASSIFICATION_INSTRUCTIONS,
    MailClassification,
    classification_request_text,
)
from tasks.runtime import (
    consume_result,
    has_batch_work,
    merge_payload,
    run_batched,
    set_progress,
)

logger = logging.getLogger(__name__)

_SELECTION = f"""
            SELECT m.id, m.from_email, m.subject, m.sent_at, m.body_text,
                   FALSE AS self_sent
            FROM email_messages m
            JOIN users u ON u.id = m.user_id
            WHERE NOT EXISTS (SELECT 1 FROM email_events e WHERE e.message_id = m.id)
              AND NOT {_SELF_SENT}
              -- Not already paid for. "No events yet" is true of a message the
              -- moment it is submitted and stays true for the hours it waits in
              -- the provider's queue, so a later sweep selected the SAME
              -- messages and submitted them again: three tasks an hour apart
              -- each carrying an identical 1,156 requests, all in flight at
              -- once. The cost is not the queue depth, it is paying repeatedly
              -- for one answer.
              --
              -- Reading the in-flight ids from the parked tasks rather than
              -- from a column, because the payload is where submit_or_collect
              -- already records them and a second copy would be a second thing
              -- to keep true.
              AND NOT EXISTS (
                  SELECT 1 FROM tasks t
                  WHERE t.kind = 'classify_mail'
                    -- Not this task. On resume it is 'running' and its own
                    -- claims are in its own payload, so without this it
                    -- excludes everything it is holding and selects nothing.
                    AND t.id <> %(task_id)s
                    AND t.status IN ('pending', 'running', 'waiting', 'awaiting_batch')
                    AND t.payload -> 'claimed_message_ids' @> to_jsonb(m.id)
              )
            -- Likely job mail first. The prefilter gates nothing, so this only
            -- changes the ORDER in which the whole mailbox is worked through -
            -- which matters because the useful results arrive sooner.
            ORDER BY m.prefilter_hit DESC NULLS LAST, m.id DESC
            LIMIT %(cap)s
            """


async def handle_classify_mail(task_id: int, payload: dict[str, Any]) -> None:
    from core.batch import structured_response_spec

    backfill = bool(payload.get("backfill"))
    shape = BACKFILL_TASK if backfill else ONGOING_TASK
    # Before this resume-first gate, an empty fresh selection skipped paid
    # collection: two completed batches retained zero recorded tokens and
    # NULL cost, leaving 2,646 messages unclassified behind their claims.
    # That historical failure becomes likelier when a backfill finishes and
    # there is normally no new mail to select.
    if has_batch_work(task_id):
        results, _ = await run_batched(task_id, shape, [])
        _record_results(task_id, results)
        return
    # Clamped rather than trusted: an enqueuer asking for the whole mailbox in
    # one task would build a spec list far larger than a wave can carry, and
    # the failure would arrive as memory pressure on a worker rather than as a
    # rejected parameter.
    per_cycle = configured_shape(shape).per_cycle
    cap = min(int(payload.get("cap") or per_cycle), per_cycle)
    # Re-classifying an EXPLICIT set of messages, for repairing events that were
    # written wrong rather than for finding ones that are missing. The set is
    # computed by the caller and recorded in the payload, so the task row says
    # exactly what was re-classified and why it was expected to be that many.
    #
    # An explicit id list rather than a "reclassify" mode carrying its own WHERE
    # clause: a predicate the handler evaluates can match the whole mailbox if
    # someone later widens it, and the cost of that mistake is a re-paid
    # backfill discovered on the bill. A list cannot grow on its own.
    #
    # Events are append-only and the latest per message wins
    # (api.mail.current.current_event), so a corrected event
    # supersedes the old one. Nothing is deleted and nothing is migrated.
    # Once per run rather than per message: the derivation is an aggregate over
    # the whole mailbox and the answer is the same for every row in the sweep.
    owner = db.query_one("SELECT user_id FROM email_messages ORDER BY id DESC LIMIT 1")
    identities = identities_for((owner or {}).get("user_id") or 0)
    logger.info(f"Task {task_id}: mailbox identities {identities}")
    requested = payload.get("message_ids")
    if not requested:
        # Only on the ordinary sweep. An explicit repair already corrects the
        # ids it was handed further down, and running both would write the same
        # correction twice - which is not wrong, exactly, but it makes "what
        # changed and why" read as two retractions of one mistake.
        healed = _heal_self_sent(identities)
        if healed:
            logger.info(f"Task {task_id}: superseded {healed} event(s) on mail the owner sent")

    if requested:
        ids = sorted({int(i) for i in requested})
        if len(ids) > cap:
            # Refused rather than truncated. Silently doing part of a repair
            # leaves the rest wrong with nothing recording which half ran.
            raise ValueError(
                f"message_ids has {len(ids)} entries, over the cap of {cap}; "
                "split it into several tasks"
            )
        rows = db.query(
            f"""
            SELECT m.id, m.from_email, m.subject, m.sent_at, m.body_text,
                   {_SELF_SENT} AS self_sent
            FROM email_messages m
            JOIN users u ON u.id = m.user_id
            WHERE m.id = ANY(%(ids)s) ORDER BY m.id
            """,
            {"ids": ids, "identities": identities},
        )
        logger.info(f"Task {task_id}: re-classifying {len(rows)} of {len(ids)} requested messages")
    else:
        rows = db.query(
            _SELECTION,
            {"cap": cap, "identities": identities, "task_id": task_id},
        )
    # Self-sent mail only reaches here through an explicit message_ids repair,
    # because the sweep excludes it. Correcting one needs no model: the header
    # already settles direction, so the event is written directly, costs
    # nothing, and supersedes the wrong one by the same latest-wins rule. Left
    # to the model these come back as offers and interviews - that is what the
    # 137 already-written events are.
    corrected = [r for r in rows if r["self_sent"]]
    rows = [r for r in rows if not r["self_sent"]]
    for row in corrected:
        mail_events.append(
            row["id"],
            "not_job_related",
            confidence="high",
            detail={"reason": "self_sent"},
            model=mail_events.SELF_SENT_RULE,
        )
    if corrected:
        logger.info(f"Task {task_id}: corrected {len(corrected)} self-sent message(s)")

    if not rows:
        set_progress(task_id, len(corrected), len(corrected), "nothing to classify")
        return
    # Claim before submission: the historical duplicate-spend incident had
    # three tasks an hour apart carrying the same 1,156 requests. Resume must
    # not replace these IDs with a fresh selection that was never submitted.
    merge_payload(task_id, {"claimed_message_ids": [r["id"] for r in rows]})

    specs = [
        structured_response_spec(
            str(r["id"]),
            CLASSIFICATION_INSTRUCTIONS,
            classification_request_text(r),
            MailClassification,
            context={"sent_at": r["sent_at"].isoformat() if r["sent_at"] else None},
        )
        for r in rows
    ]
    set_progress(task_id, 0, len(specs), "mail classification submitted")
    results, _ = await run_batched(task_id, shape, specs)

    _record_results(task_id, results)


def _record_results(task_id: int, results: list) -> None:
    # One malformed row must not lose 4,999 other paid classifications in a
    # 5,000-result batch, the scale behind the original per-row guard. Durable
    # receipts now preserve unread results on storage failure and skip earlier
    # acknowledgments on retry; do not restore a catch that drops failed writes.
    for res in results:
        with consume_result(task_id, res) as receipt:
            if not receipt.pending:
                continue
            if res.request is None or res.request.context is None:
                receipt.outcome = "unknown_request"
                continue
            if not res.custom_id.isdigit() or not db.query_one(
                "SELECT 1 FROM email_messages WHERE id = %s", (int(res.custom_id),)
            ):
                receipt.outcome = "subject_missing"
                continue
            _record_result(res, receipt)
    done, total = progress_counts(task_id)
    set_progress(task_id, done, total, "mail classified")


def _record_result(res, receipt) -> None:
    key = res.custom_id
    if res.error or not res.text:
        receipt.outcome = "failed"
        return
    try:
        parsed = MailClassification.model_validate_json(res.text)
    except ValueError:
        logger.warning(f"mail classify: unparsable output for message {key}")
        receipt.outcome = "invalid_output"
        return
    # Everything that can raise happens BEFORE anything is written, so a
    # half-parsed result cannot produce a half-written row that later looks
    # like a completed classification.
    when = parse_when(
        parsed.deadline,
        sent_at=datetime.datetime.fromisoformat(res.request.context["sent_at"])
        if res.request.context.get("sent_at")
        else None,
        kind=parsed.kind,
    )
    # An interview that has been scheduled is an appointment, not something
    # to act by. Until now every one of these went into deadline_at with
    # its time stripped, while occurred_at - which mail_pipeline already
    # reads - was never written at all.
    occurred_at: datetime.datetime | None = None
    deadline_at: datetime.datetime | None = None
    year_inferred = False
    # Mail that is not about a job has no job deadline, whatever date it
    # states. This is the model's own answer applied to its own other
    # answer, not a second judgement: it already said the message is not
    # job related, so the date it found is a marketing expiry, a newsletter
    # RSVP or a tuition date.
    #
    # The historical corpus audit found 1,409 of 2,128 recorded deadlines on
    # non-job mail. These dates fed action items and "quiet for 60+ days";
    # they are evidence for this guard, not a count of the current corpus.
    if when is not None and parsed.kind != "not_job_related":
        year_inferred = when.year_inferred
        if is_appointment(parsed.kind):
            occurred_at = when.at
        else:
            deadline_at = when.at
    detail: dict[str, Any] = {"company": parsed.company, "role_title": parsed.role_title}
    if parsed.deadline:
        # The raw string is kept whether or not it parsed. Keeping only the
        # successes means the failures cannot be studied - which is exactly
        # the position this parser was rewritten from, with no record of
        # what it had been unable to read.
        detail["when_raw"] = parsed.deadline
        detail["when_dropped_as_not_job_related"] = (
            parsed.kind == "not_job_related" and when is not None
        )
        detail["when_precision"] = (
            None if when is None else ("instant" if when.is_instant else "date")
        )
    mail_events.append(
        int(key),
        parsed.kind,
        confidence=parsed.confidence,
        occurred_at=occurred_at,
        deadline_at=deadline_at,
        # Inferred if the model said so, OR if we resolved a missing year
        # ourselves. Either way it is not a date the email stated outright.
        deadline_inferred=deadline_at is not None
        and (year_inferred or not parsed.deadline_is_explicit),
        detail=detail,
        model=res.model,
    )
    receipt.outcome = "written"
