"""Attach classified mail to applications, and create the applications it implies.

The matcher in `api.mail_match` has always worked; it had nothing to work
against. `applications` was empty, so every tier returned UNMATCHED and the
whole outcome side of the product stayed dark while 67k messages classified.

Two populations feed it, and the order they run in is load-bearing:

1. The tracker's own applications, which have a job and a date.
2. Applications that only email knows about - the 2022-era ones whose posting
   was never in this catalog and never will be.

Tracker first, then match, then create from what stayed unmatched. Creating
before matching would manufacture a second application at a company that
already had one, and `_by_company` refuses to choose between two candidates -
so the wrong order poisons the matcher permanently and silently.
"""

from __future__ import annotations

import logging
from typing import Any

from api import db, mail_match
from api.mail_pipeline import sync_action_items
from api.tasks.runtime import _set_progress

logger = logging.getLogger("jobtracker_worker")


# Kinds that are evidence the user applied. An employer does not acknowledge,
# reject, or schedule an interview for someone who never applied.
#
# recruiter_outreach is deliberately absent: an approach is not an
# application, and it is the one kind that routinely arrives from companies
# the user has no relationship with. Creating applications from it would
# invent a job search that did not happen.
APPLIED_KINDS = frozenset(
    {
        "acknowledgement",
        "rejection",
        "assessment_invite",
        "interview_invite",
        "interview_scheduled",
        "info_request",
        "offer",
        "position_closed",
    }
)

# The tracker statuses that mean an application exists. "No Longer Interested"
# is excluded: it is the status this user assigns to postings they decided
# against, and 634 of them would otherwise become applications never made.
APPLIED_STATUSES = ("Application Submitted", "Follow-up")

DERIVED = "derived"

# A derived application needs a role. An application is a (company, role) pair,
# and mail that names an employer but no role is half an entity - a careers
# newsletter, an event announcement, a mass update. Recording it as an
# application would put a row in the funnel that nothing can ever resolve.
#
# The volume cap is calibrated per user against their own tracker, because
# "too many applications at one employer" has no absolute value: this user
# really did apply to Tesla 43 times, so any fixed ceiling below that would
# have deleted real history. What it catches is mail from an organisation the
# user is INVOLVED with rather than applying to - a hackathon they help run
# generates hundreds of genuine "interview scheduled" messages where they are
# the interviewer. No event-kind rule separates those; the volume does.
#
# Over the cap, nothing is created and the events stay unmatched, which is the
# honest state: we looked, and we are not confident enough to name an
# application. The evidence remains in the log for a later adjudication pass.
CAP_FLOOR = 15


def seed_from_tracker(user_id: int) -> int:
    """One application per tracked application, carrying its job.

    Idempotent on (user_id, job_id) so a re-run adds nothing. company and
    title are copied rather than joined at read time because the job row can
    be deleted - `applications.job_id` is ON DELETE SET NULL - and an
    application that loses its posting must not also lose its identity.
    """
    rows = db.query(
        """
        INSERT INTO applications (user_id, job_id, company_name, title,
                                  source_provenance, applied_at)
        SELECT uj.user_id, uj.job_id, j.company, j.title, 'tracker', uj.date_applied
        FROM user_jobs uj
        JOIN jobs j ON j.id = uj.job_id
        WHERE uj.user_id = %(user)s
          AND uj.status = ANY(%(statuses)s)
          AND NOT EXISTS (
              SELECT 1 FROM applications a
              WHERE a.user_id = uj.user_id AND a.job_id = uj.job_id
          )
        RETURNING id
        """,
        {"user": user_id, "statuses": list(APPLIED_STATUSES)},
    )
    return len(rows)


def _unmatched_applied_messages(user_id: int) -> list[dict[str, Any]]:
    """Messages whose current event says the user applied, that matched nothing.

    Current event, not any event: a message reclassified away from a rejection
    must stop counting as evidence of an application, the same retraction rule
    `events_for` implements.
    """
    return db.query(
        """
        WITH current_event AS (
            SELECT DISTINCT ON (message_id) message_id, kind, detail
            FROM email_events ORDER BY message_id, id DESC
        ),
        current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id
            FROM application_matches ORDER BY message_id, id DESC
        )
        SELECT m.id, m.provider_thread_id, m.sent_at,
               e.detail->>'company' AS company,
               e.detail->>'role_title' AS title
        FROM email_messages m
        JOIN current_event e ON e.message_id = m.id
        LEFT JOIN current_match cm ON cm.message_id = m.id
        WHERE m.user_id = %(user)s
          AND e.kind = ANY(%(kinds)s)
          AND e.detail->>'company' IS NOT NULL
          AND cm.application_id IS NULL
        ORDER BY m.sent_at NULLS LAST, m.id
        """,
        {"user": user_id, "kinds": list(APPLIED_KINDS)},
    )


def _existing_companies(user_id: int) -> set[str]:
    rows = db.query("SELECT company_name FROM applications WHERE user_id = %s", (user_id,))
    return {mail_match.norm_company(r["company_name"]) for r in rows} - {""}


def derived_cap(user_id: int) -> int:
    """The most applications this user has ever made at one employer.

    Read from the tracker, which is ground truth: they recorded these
    themselves. CAP_FLOOR keeps a user with no tracker history from deriving
    nothing at all, and is the 99th percentile of this user's per-company
    counts rather than a round number.
    """
    row = db.query_one(
        """
        SELECT coalesce(max(n), 0) AS mx FROM (
            SELECT count(*) AS n FROM applications
            WHERE user_id = %s AND source_provenance = 'tracker'
            GROUP BY lower(company_name)
        ) s
        """,
        (user_id,),
    )
    return max(int(row["mx"]) if row else 0, CAP_FLOOR)


def seed_from_mail(user_id: int) -> tuple[int, int]:
    """Applications that only the mail knows about, matched as they are created.

    Only where the user has NO application at that company. A failed match at
    a company that IS tracked means the window or the title disagreed, and the
    answer to that is adjudication, not a second application - inventing one
    would split one real application's history across two rows and leave both
    permanently ambiguous to `_by_company`.

    Grouping is by thread where the provider gave us one, falling back to
    company and role.

    That 99% Outlook "thread coverage" this once claimed was not threading. The
    .olm importer filled provider_thread_id from ThreadTopic, a normalised
    SUBJECT, so every ATS autoresponder sharing a subject line grouped as one
    conversation and derived a single application - "Nittany Lion Careers
    Application Confirmation" alone was 56 messages across 32 employers. Outlook
    mail now carries no thread id at all and its topic lives in thread_topic,
    so the (company, role) fallback is the path for it, as it always should
    have been. Real thread ids remain on Takeout (first References entry) and
    Gmail (its own threadId).
    """
    known = _existing_companies(user_id)
    cap = derived_cap(user_id)

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    company_of: dict[tuple[str, ...], str] = {}
    # Evidence that names the employer but not the role. It cannot key a group
    # - there is nothing to key it on - but it is still evidence of when the
    # user applied, and leaving it out of applied_at is what made the derived
    # date veto the very messages it was derived from.
    titleless: dict[str, list[dict[str, Any]]] = {}
    for msg in _unmatched_applied_messages(user_id):
        company = mail_match.norm_company(msg["company"])
        title = mail_match.norm_company(msg["title"])
        if not company or company in known:
            continue
        if not title:
            titleless.setdefault(company, []).append(msg)
            continue
        thread = msg["provider_thread_id"]
        key = ("t", thread) if thread else ("c", company, title)
        groups.setdefault(key, []).append(msg)
        company_of[key] = company

    counts: dict[str, int] = {}
    for key in groups:
        counts[company_of[key]] = counts.get(company_of[key], 0) + 1
    over = {company for company, n in counts.items() if n > cap}
    for company in sorted(over):
        logger.info(
            f"user {user_id}: {counts[company]} candidate applications at {company} "
            f"exceeds this user's cap of {cap}; deriving none, evidence stays unmatched"
        )

    created = matched = 0
    for key, messages in groups.items():
        if company_of[key] in over:
            continue
        first = messages[0]
        # applied_at over ALL the evidence at this group, not just the part
        # that happened to carry a role title.
        #
        # Taking the earliest TITLED message and then using it as a floor made
        # the date veto its own earlier evidence: 159 of 161 blocked messages
        # have no role_title, and the margins are absurd - Battelle by four
        # seconds, Werfen by fifty-four. The earliest evidence was discarded
        # and then used to rule itself out.
        #
        # Only where the company has exactly one group. With several, a
        # title-less message could belong to any of them, and quietly assigning
        # it to one would replace a wrong answer with a different wrong answer.
        # It stays unmatched, which is the honest state and re-runnable.
        evidence = messages
        if counts[company_of[key]] == 1:
            evidence = messages + titleless.get(company_of[key], [])
        applied_at = min(
            (m["sent_at"] for m in evidence if m["sent_at"] is not None),
            default=first["sent_at"],
        )
        # Conditional on the anchor message STILL being unmatched, in one
        # statement, because two workers can hold this task at once.
        #
        # That is not hypothetical: a slow worker kept running this handler
        # after the reaper had already requeued its task and another worker had
        # finished it. Nothing stopped the loser writing a second copy of every
        # application the winner had just created - and a duplicate here does
        # not merely add a row. `_by_company` refuses to choose between two
        # applications at one employer, so it would make every future message
        # at that company permanently unmatchable, silently.
        #
        # A unique constraint cannot express this: company and title are
        # nullable free text, and two genuine applications to the same role at
        # the same company are legal. The condition that actually matters is
        # whether this evidence has already been used.
        row = db.query_one(
            """
            INSERT INTO applications (user_id, job_id, company_name, title,
                                      source_provenance, applied_at)
            SELECT %(user)s, NULL, %(company)s, %(title)s, 'email', %(sent)s
            WHERE NOT EXISTS (
                SELECT 1 FROM application_matches am
                WHERE am.message_id = %(msg)s
                  AND am.application_id IS NOT NULL
                  AND am.id = (
                      SELECT max(id) FROM application_matches
                      WHERE message_id = %(msg)s
                  )
            )
            RETURNING id
            """,
            {
                "user": user_id,
                "company": first["company"],
                "title": first["title"],
                "sent": applied_at,
                "msg": first["id"],
            },
        )
        if row is None:
            # Another worker got there first. Its match is authoritative and
            # this group is already represented.
            continue
        created += 1
        for msg in messages:
            mail_match.record(
                msg["id"],
                mail_match.Match(row["id"], DERIVED, "high", "application derived from this mail"),
            )
            matched += 1
    return created, matched


def match_pending(user_id: int, *, limit: int | None = None) -> dict[str, int]:
    """Run the tiers over every job-related message with no match recorded.

    UNMATCHED is recorded, not skipped. "We looked and found nothing" is a
    different state from "we never looked", and only the first one lets a
    later re-run be measured against this one.
    """
    rows = db.query(
        """
        WITH current_event AS (
            SELECT DISTINCT ON (message_id) message_id, kind, detail
            FROM email_events ORDER BY message_id, id DESC
        ),
        current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id
            FROM application_matches ORDER BY message_id, id DESC
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
        ORDER BY m.sent_at NULLS LAST, m.id
        """
        + ("LIMIT %(limit)s" if limit else ""),
        {"user": user_id, "limit": limit},
    )
    counts: dict[str, int] = {}
    for row in rows:
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
        counts[match.method] = counts.get(match.method, 0) + 1
    return counts


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
        """
        WITH current_event AS (
            SELECT DISTINCT ON (message_id) message_id, kind
            FROM email_events ORDER BY message_id, id DESC
        ),
        current_match AS (
            SELECT DISTINCT ON (message_id) message_id, application_id
            FROM application_matches ORDER BY message_id, id DESC
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


async def handle_match_mail(task_id: int, payload: dict[str, Any]) -> None:
    """Match every user's mail, or one user's when asked.

    Scheduled runs carry no user_id: matching is per-user by construction (the
    candidate set is that user's applications) but the schedule is fleet-wide,
    and a task that silently did only user 1 would look identical to one that
    did everybody.
    """
    requested = payload.get("user_id")
    if requested:
        user_ids = [int(requested)]
    else:
        # Users with mail OR with tracked applications. Scoping to mail alone
        # would leave anyone who has not connected Gmail with an empty
        # applications table and therefore no funnel at all, which reads as
        # "you have applied to nothing" rather than "we have no mail for you".
        user_ids = [
            r["user_id"]
            for r in db.query(
                """
                SELECT user_id FROM email_messages
                UNION
                SELECT user_id FROM user_jobs WHERE status = ANY(%s)
                ORDER BY user_id
                """,
                (list(APPLIED_STATUSES),),
            )
        ]
    if not user_ids:
        _set_progress(task_id, 0, 0, "no mail to match")
        return

    limit = payload.get("limit")
    totals = {"tracked": 0, "derived": 0, "matched": 0, "detached": 0, "opened": 0, "resolved": 0}
    for index, user_id in enumerate(user_ids):
        _set_progress(task_id, index, len(user_ids), f"matching user {user_id}")
        totals["tracked"] += seed_from_tracker(user_id)
        totals["detached"] += detach_unattachable(user_id)
        counts = match_pending(user_id, limit=int(limit) if limit else None)
        totals["matched"] += sum(counts.values())
        created, _ = seed_from_mail(user_id)
        totals["derived"] += created
        for app in db.query("SELECT id FROM applications WHERE user_id = %s", (user_id,)):
            result = sync_action_items(app["id"])
            totals["opened"] += result["opened"]
            totals["resolved"] += result["resolved"]

    summary = (
        f"{len(user_ids)} user(s): {totals['tracked']} tracked + {totals['derived']} derived "
        f"applications, {totals['matched']} messages matched, "
        f"{totals['detached']} detached, "
        f"{totals['opened']} items opened, {totals['resolved']} resolved"
    )
    logger.info(f"Task {task_id}: {summary}")
    _set_progress(task_id, len(user_ids), len(user_ids), summary)
