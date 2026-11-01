"""Derive applications from unmatched mail and maintain their date floors."""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.mail import match as mail_match
from api.mail.current import current_event, current_match
from api.mail.match_decision import DERIVED

logger = logging.getLogger(__name__)


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


def _unmatched_applied_messages(user_id: int) -> list[dict[str, Any]]:
    """Messages whose current event says the user applied, that matched nothing.

    Current event, not any event: a message reclassified away from a rejection
    must stop counting as evidence of an application, the same retraction rule
    `events_for` implements.
    """
    return db.query(
        f"""
        WITH current_event AS (
            {current_event("kind", "detail")}
        ),
        current_match AS (
            {current_match("application_id")}
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
    """Create applications from unmatched mail at untracked employers.

    A failed match at a tracked employer needs adjudication. Creating another
    application there could split one history and make `_by_company` ambiguous.

    Group by a real provider thread ID when present, otherwise by company and
    role. Outlook ThreadTopic is a normalized subject, not a thread ID. An
    earlier import grouped 56 messages from 32 employers under one shared
    subject. Outlook now uses the company and role fallback; Gmail and Takeout
    retain their real thread identifiers.
    """
    known = _existing_companies(user_id)
    cap = derived_cap(user_id)

    candidate_groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    company_by_group: dict[tuple[str, ...], str] = {}
    # Evidence that names the employer but not the role. It cannot key a group
    # - there is nothing to key it on - but it is still evidence of when the
    # user applied, and leaving it out of applied_at is what made the derived
    # date veto the very messages it was derived from.
    messages_without_title_by_company: dict[str, list[dict[str, Any]]] = {}
    for msg in _unmatched_applied_messages(user_id):
        company = mail_match.norm_company(msg["company"])
        title = mail_match.norm_company(msg["title"])
        if not company or company in known:
            continue
        if not title:
            messages_without_title_by_company.setdefault(company, []).append(msg)
            continue
        thread = msg["provider_thread_id"]
        key = ("t", thread) if thread else ("c", company, title)
        candidate_groups.setdefault(key, []).append(msg)
        company_by_group[key] = company

    group_count_by_company: dict[str, int] = {}
    for key in candidate_groups:
        group_count_by_company[company_by_group[key]] = (
            group_count_by_company.get(company_by_group[key], 0) + 1
        )
    over_cap_companies = {company for company, n in group_count_by_company.items() if n > cap}
    for company in sorted(over_cap_companies):
        logger.info(
            f"user {user_id}: {group_count_by_company[company]} candidate applications at {company} "
            f"exceeds this user's cap of {cap}; deriving none, evidence stays unmatched"
        )

    created_count = matched_message_count = 0
    for key, messages in candidate_groups.items():
        if company_by_group[key] in over_cap_companies:
            continue
        anchor_message = messages[0]
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
        if group_count_by_company[company_by_group[key]] == 1:
            evidence = messages + messages_without_title_by_company.get(company_by_group[key], [])
        applied_at = min(
            (m["sent_at"] for m in evidence if m["sent_at"] is not None),
            default=anchor_message["sent_at"],
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
                "company": anchor_message["company"],
                "title": anchor_message["title"],
                "sent": applied_at,
                "msg": anchor_message["id"],
            },
        )
        if row is None:
            # Another worker got there first. Its match is authoritative and
            # this group is already represented.
            continue
        created_count += 1
        for msg in messages:
            mail_match.record(
                msg["id"],
                mail_match.MatchDecision(
                    row["id"], DERIVED, "high", "application derived from this mail"
                ),
            )
            matched_message_count += 1
    return created_count, matched_message_count


def recompute_derived_floors(user_id: int) -> int:
    """Lower an email-derived applied_at that was computed from less evidence
    than now exists. Returns how many moved.

    seed_from_mail only ever creates applications for groups that do not
    already have one, so the rule it applies is the rule at the moment of
    creation. When that rule was corrected to compute the floor over ALL the
    evidence at a group rather than only the messages carrying a role title,
    the correction reached nothing that already existed - 1,813 applications
    kept floors taken from the titled subset, and 383 messages stayed blocked
    by them.

    A fix that cannot reach its own population reads as done and is not, so
    this runs every sweep rather than once. It is idempotent by construction:
    a floor that is already the earliest evidence does not move, so a sweep
    that changes nothing writes nothing and the second run is a no-op.

    ONLY EVER LOWERS. Raising a floor would invalidate matches already made
    against it, and there is no evidence that could justify it - a later
    message is not proof the application started later.
    """
    derived = db.query(
        "SELECT id, company_name, applied_at FROM applications "
        "WHERE user_id = %s AND source_provenance = 'email' AND applied_at IS NOT NULL",
        (user_id,),
    )
    if not derived:
        return 0

    earliest_attached: dict[int, Any] = {}
    for row in db.query(
        f"""
        WITH cm AS ({current_match("application_id")})
        SELECT cm.application_id AS app_id, min(m.sent_at) AS earliest
        FROM cm JOIN email_messages m ON m.id = cm.message_id
        WHERE cm.application_id IS NOT NULL AND m.user_id = %s AND m.sent_at IS NOT NULL
        GROUP BY 1
        """,
        (user_id,),
    ):
        earliest_attached[row["app_id"]] = row["earliest"]

    # Unattached evidence, folded in only where the company has exactly one
    # derived application. With several, a message naming no role could belong
    # to any of them, and lowering one floor on its say-so is the guess
    # seed_from_mail already refuses to make.
    by_company: dict[str, list[dict[str, Any]]] = {}
    for app in derived:
        by_company.setdefault(mail_match.norm_company(app["company_name"]), []).append(app)
    earliest_loose: dict[str, Any] = {}
    for row in _unmatched_applied_messages(user_id):
        key = mail_match.norm_company(row["company"])
        if not key or row["sent_at"] is None:
            continue
        if earliest_loose.get(key) is None or row["sent_at"] < earliest_loose[key]:
            earliest_loose[key] = row["sent_at"]

    moved = 0
    for app in derived:
        candidates = [app["applied_at"]]
        if (attached := earliest_attached.get(app["id"])) is not None:
            candidates.append(attached)
        key = mail_match.norm_company(app["company_name"])
        if len(by_company.get(key, [])) == 1 and earliest_loose.get(key) is not None:
            candidates.append(earliest_loose[key])
        floor = min(candidates)
        if floor < app["applied_at"]:
            db.execute(
                "UPDATE applications SET applied_at = %s WHERE id = %s AND applied_at > %s",
                (floor, app["id"], floor),
            )
            moved += 1
    if moved:
        logger.info(f"user {user_id}: lowered {moved} derived applied_at floor(s)")
    return moved
