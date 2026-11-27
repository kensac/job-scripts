"""Derive applications from unmatched mail and maintain their date floors."""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.mail import match as mail_match
from api.mail.current import current_event, current_match
from api.mail.match_decision import DERIVED

logger = logging.getLogger(__name__)

# Event kinds that imply an application (excludes recruiter_outreach).
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

# Minimum per-company derivation cap when tracker history is smaller. Companies
# exceeding the user's cap are left unmatched rather than creating bulk rows.
CAP_FLOOR = 15


def _unmatched_applied_messages(user_id: int) -> list[dict[str, Any]]:
    """Unmatched messages whose current event is in APPLIED_KINDS and names a company."""
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
    """Max applications this user recorded at one company on their tracker, floored at CAP_FLOOR."""
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

    Skips companies that already have an application (to keep `_by_company`
    unambiguous) and groups messages by provider thread ID when present, else by
    normalized company and role title.
    """
    known = _existing_companies(user_id)
    cap = derived_cap(user_id)

    candidate_groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    company_by_group: dict[tuple[str, ...], str] = {}
    # Messages naming only a company cannot key a group, but still contribute to
    # applied_at when the company has a single derived group.
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
        # Fold in title-less company mail only when the company has one group,
        # so applied_at does not veto earlier untitled messages for that role.
        evidence = messages
        if group_count_by_company[company_by_group[key]] == 1:
            evidence = messages + messages_without_title_by_company.get(company_by_group[key], [])
        applied_at = min(
            (m["sent_at"] for m in evidence if m["sent_at"] is not None),
            default=anchor_message["sent_at"],
        )
        # Guard against concurrent workers claiming the same anchor message;
        # duplicate applications at one employer would break `_by_company`.
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
    """Lower email-derived `applied_at` values when earlier evidence exists.

    Idempotent and strictly monotonic (never raises a floor, which could
    invalidate existing matches). Returns the number of rows lowered.
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

    # Fold in unmatched company mail only when the company has one derived application.
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
