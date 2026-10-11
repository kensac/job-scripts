"""Match pending messages and detach kinds that cannot belong to applications."""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable

from api import db
from api.mail import match as mail_match
from api.mail.current import current_event, current_match

logger = logging.getLogger("tasks.mail.matching.handler")

# Progress is written every this many messages or applications, because a
# single user's sweep is most of a run: 4,853 messages and 2,587 applications
# for the one user in production on 2026-10-04, which took 4 minutes on a host
# beside the database and 110 on one 100 ms from it. Written once per user,
# that whole run looked like one stalled step. 110 minutes over those 7,440
# items is under 1 s an item, so this is a write every two minutes or less on
# the far host; per item it would add a round trip and an event publish to
# every message.
PROGRESS_EVERY = 100


def match_pending(
    user_id: int,
    *,
    limit: int | None = None,
    since: datetime.datetime | None = None,
    progress: Callable[[int, int], None] | None = None,
    progress_every: int = PROGRESS_EVERY,
) -> dict[str, int]:
    """Run the tiers over every job-related message with no match recorded.

    UNMATCHED is recorded, not skipped. "We looked and found nothing" is a
    different state from "we never looked", and only the first one lets a
    later re-run be measured against this one.
    """
    rows = db.query(
        f"""
        WITH current_event AS (
            {current_event("kind", "detail", "created_at")}
        ),
        current_match AS (
            {current_match("application_id", "created_at")}
        )
        SELECT m.id, m.body_text, m.sent_at, e.kind,
               e.detail->>'company' AS company, e.detail->>'role_title' AS title
        FROM email_messages m
        JOIN current_event e ON e.message_id = m.id
        LEFT JOIN current_match cm ON cm.message_id = m.id
        WHERE m.user_id = %(user)s
          AND e.kind <> 'not_job_related'
          -- application_id IS NULL, not message_id IS NULL. The latter meant
          -- "never looked", so a message that once came back `unmatched` was
          -- frozen against whatever applications existed at that moment -
          -- directly against this module's own claim that a message which
          -- could not be matched in March can match in September. 8,462
          -- unmatched verdicts were written before 1,779 applications were
          -- created, and nothing would ever have revisited them.
          --
          -- Re-deciding is safe because it only ever reaches messages holding
          -- no application, and `record` suppresses a verdict identical to the
          -- one standing, so a sweep that changes nothing writes nothing.
          AND cm.application_id IS NULL
          -- ...and only where the answer could actually have moved since we
          -- last looked. Re-deciding every unmatched message every sweep is
          -- correct and ruinous: that set does not shrink, so a three-minute
          -- handler became a sixty-minute one holding a third of the fleet
          -- while the cheap work queued behind it.
          --
          -- Two things can change a verdict and nothing else can. A new
          -- application may be the candidate that was missing - which is the
          -- whole finding: 289 of 297 recoverable messages have an application
          -- created after their verdict was written. Or the message was
          -- reclassified, so a different rule now applies, which is what
          -- leaves a recruiter approach holding a stale `unmatched`.
          --
          -- "Since we last looked" is the later of the verdict and the start
          -- of the last finished sweep, not the verdict alone. `record` does
          -- not append a verdict identical to the standing one, so the
          -- verdict's timestamp never moves for a message that keeps coming
          -- back `unmatched`: every application newer than it re-selected the
          -- message on every run, forever. Measured on production for the 14
          -- days to 2026-10-10: 245 runs, 200.5 worker-hours, each deciding
          -- the same 4,852 messages again, and 244 of them changed nothing.
          AND (
              cm.message_id IS NULL
              OR e.created_at > GREATEST(cm.created_at, %(since)s::timestamptz)
              OR EXISTS (
                  SELECT 1 FROM applications a
                  WHERE a.user_id = %(user)s
                    AND a.created_at > GREATEST(cm.created_at, %(since)s::timestamptz)
              )
          )
        ORDER BY m.sent_at NULLS LAST, m.id
        """
        + ("LIMIT %(limit)s" if limit else ""),
        {"user": user_id, "limit": limit, "since": since},
    )
    decisions_by_method: dict[str, int] = {}
    for index, row in enumerate(rows):
        if progress and index % progress_every == 0:
            progress(index, len(rows))
        if row["kind"] in mail_match.UNATTACHABLE_KINDS:
            match = mail_match.Match(
                None,
                mail_match.NOT_AN_APPLICATION,
                "none",
                f"{row['kind']} describes the person, not an application",
            )
        else:
            match = mail_match.match_message(
                user_id,
                body=row["body_text"],
                company=row["company"],
                title=row["title"],
                sent_at=row["sent_at"],
            )
        mail_match.record(row["id"], match)
        decisions_by_method[match.method] = decisions_by_method.get(match.method, 0) + 1
    return decisions_by_method


def detach_unattachable(user_id: int) -> int:
    """Correct messages already attached that never should have been.

    Matching is append-only and `match_pending` only considers messages with no
    current match, so a rule change does not reach what the old rule already
    decided. Without this the fix applies to new mail and silently leaves the
    existing wrong attachments in place - which is how a corrected matcher
    still reports the numbers that prompted the correction.

    Idempotent: a message already detached has no current application and is
    not selected again.
    """
    rows = db.query(
        f"""
        WITH current_event AS (
            {current_event("kind")}
        ),
        current_match AS (
            {current_match("application_id")}
        )
        SELECT m.id, e.kind FROM email_messages m
        JOIN current_event e ON e.message_id = m.id
        JOIN current_match cm ON cm.message_id = m.id
        WHERE m.user_id = %(user)s
          AND e.kind = ANY(%(kinds)s)
          AND cm.application_id IS NOT NULL
        """,
        {"user": user_id, "kinds": sorted(mail_match.UNATTACHABLE_KINDS)},
    )
    for row in rows:
        mail_match.record(
            row["id"],
            mail_match.Match(
                None,
                mail_match.NOT_AN_APPLICATION,
                "none",
                f"{row['kind']} describes the person, not an application",
            ),
        )
    if rows:
        logger.info(f"user {user_id}: detached {len(rows)} message(s) that describe the person")
    return len(rows)
