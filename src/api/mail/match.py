"""Attach emails to applications via ordered matching tiers.

Tiers run cheapest and most certain first and never guess: ambiguous cases stay
UNMATCHED. `application_matches` is append-only so sweeps can reconsider
messages as new applications appear.
"""

from __future__ import annotations

import datetime
import logging
import re

from pydantic import BaseModel

from api import db
from api.mail.match_decision import (
    ADJUDICATED as ADJUDICATED,
)
from api.mail.match_decision import (
    ATS_COMPANY,
    COMPANY_TITLE,
    EXACT_LINK,
    MatchDecision,
)
from api.mail.match_decision import (
    DETACHED as DETACHED,
)
from api.mail.match_decision import (
    HUMAN_METHODS as HUMAN_METHODS,
)
from api.mail.match_decision import (
    MANUAL as MANUAL,
)
from api.mail.match_decision import (
    NOT_AN_APPLICATION as NOT_AN_APPLICATION,
)
from api.mail.match_decision import (
    UNMATCHED as UNMATCHED,
)
from api.mail.match_sweeps import changed_since as changed_since
from api.mail.match_sweeps import last_sweep_start as last_sweep_start
from core.fetching.ats import canonicalize

logger = logging.getLogger(__name__)

# Event kinds that are never attached to an application (matches derived.APPLIED_KINDS).
UNATTACHABLE_KINDS = frozenset({"recruiter_outreach"})

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


class CurrentMatch(BaseModel):
    """The match row in force for a message.

    Declared beside the query that reads it, because an admin correction
    returns it verbatim and the columns were a `SELECT *` - so what reached
    the client was whatever the table happened to hold. A null
    `application_id` is a recorded outcome rather than an absence: `method`
    says which of the two it is."""

    id: int
    message_id: int
    application_id: int | None
    method: str
    confidence: str | None
    rationale: str | None
    # NULL means the matcher wrote this row; an id means a person did.
    actor_user_id: int | None
    created_at: datetime.datetime


def urls_in(body: str | None) -> list[str]:
    return _URL_RE.findall(body or "")


def canonical_urls(body: str | None) -> set[str]:
    """Canonical ATS posting URLs extracted from `body`."""
    found = set()
    for url in urls_in(body):
        canonical = canonicalize(url.rstrip(".,);"))
        if canonical:
            found.add(canonical)
    return found


def _by_exact_link(user_id: int, body: str | None) -> MatchDecision | None:
    canon = canonical_urls(body)
    if not canon:
        return None
    rows = db.query(
        """
        SELECT a.id, j.url
        FROM applications a JOIN jobs j ON j.id = a.job_id
        WHERE a.user_id = %s AND a.job_id IS NOT NULL
        """,
        (user_id,),
    )
    for row in rows:
        job_canonical = canonicalize(row["url"]) or row["url"]
        if job_canonical in canon:
            return MatchDecision(
                row["id"],
                EXACT_LINK,
                "high",
                f"message links to {job_canonical}",
            )
    return None


def norm_company(value: str | None) -> str:
    """Normalize free-text company or role names so 'Stripe' and 'Stripe, Inc.' match."""
    text = (value or "").lower().strip()
    text = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|sa|nv)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


# Shared by both company tiers: mail cannot predate an application. Tracker
# dates carry 1 day of slack because they are timezone-less dates cast to UTC
# midnight; email-derived timestamps are exact instants and get 0 slack.
_NOT_BEFORE_APPLYING = """
    (a.applied_at IS NULL OR %s::timestamptz IS NULL
     OR a.applied_at - CASE WHEN a.source_provenance = 'tracker'
                            THEN interval '1 day' ELSE interval '0' END
        <= %s::timestamptz)
"""

_INTERNSHIP_WORDS = frozenset({"intern", "interns", "internship", "internships", "coop"})


def _employment_type_contradicts(mail_title: str | None, app_title: str | None) -> bool:
    """True when both titles are present and only one names an internship."""
    if not mail_title or not app_title:
        return False
    mail_words = set(re.findall(r"[a-z0-9]+", mail_title.lower()))
    app_words = set(re.findall(r"[a-z0-9]+", app_title.lower()))
    return bool(mail_words & _INTERNSHIP_WORDS) != bool(app_words & _INTERNSHIP_WORDS)


def _by_company(
    user_id: int, company: str | None, sent_at, title: str | None = None
) -> MatchDecision | None:
    """Match a single unambiguous application at `company` inside the date window."""
    key = norm_company(company)
    if not key:
        return None
    rows = db.query(
        """
        SELECT a.id, a.company_name, a.title, a.applied_at
        FROM applications a
        WHERE a.user_id = %s
          AND """
        + _NOT_BEFORE_APPLYING
        + """
        """,
        (user_id, sent_at, sent_at),
    )
    candidates = [r for r in rows if norm_company(r["company_name"]) == key]
    if len(candidates) == 1 and _employment_type_contradicts(title, candidates[0]["title"]):
        # Refuse rather than attach an internship email to a full-time role (or vice versa).
        return None
    if len(candidates) == 1:
        return MatchDecision(
            candidates[0]["id"],
            ATS_COMPANY,
            "medium",
            f"single application at {candidates[0]['company_name']}",
        )
    if len(candidates) > 1:
        logger.debug(f"{len(candidates)} candidates at {company} for user {user_id}")
    return None


def _by_company_and_title(
    user_id: int, company: str | None, title: str | None, sent_at=None
) -> MatchDecision | None:
    """Match a single application by normalized company and title inside the date window."""
    key, role = norm_company(company), norm_company(title)
    if not key or not role:
        return None
    rows = db.query(
        """
        SELECT a.id, a.company_name, a.title
        FROM applications a
        WHERE a.user_id = %s
          AND """
        + _NOT_BEFORE_APPLYING,
        (user_id, sent_at, sent_at),
    )
    candidates = [
        r
        for r in rows
        if norm_company(r["company_name"]) == key and norm_company(r["title"]) == role
    ]
    if len(candidates) == 1:
        return MatchDecision(
            candidates[0]["id"],
            COMPANY_TITLE,
            "medium",
            f"company and title match: {candidates[0]['title']}",
        )
    return None


def match_message(
    user_id: int,
    *,
    body: str | None,
    company: str | None,
    title: str | None,
    sent_at=None,
) -> MatchDecision:
    """Run matching tiers in order and return the first unambiguous match or UNMATCHED."""
    for finder in (
        lambda: _by_exact_link(user_id, body),
        lambda: _by_company(user_id, company, sent_at, title),
        lambda: _by_company_and_title(user_id, company, title, sent_at),
    ):
        found = finder()
        if found is not None:
            return found
    return MatchDecision.unmatched("no candidate matched")


def record(message_id: int, match: MatchDecision, *, actor_user_id: int | None = None) -> None:
    """Append a match decision and advance `email_messages.current_match_id`.

    When `actor_user_id` is None (automated matcher):
    - Never overturns an existing human decision (enforced here at write time
      against races with sweep selection).
    - Skips appending if the decision fields repeat the current match.

    Human writes always append, even when agreeing with the current match.
    """
    if actor_user_id is None:
        current = latest(message_id)
        if current is not None and current.actor_user_id is not None:
            logger.debug(f"message {message_id}: a person decided this; matcher stands down")
            return
        if current is not None and match.repeats(
            current.application_id, current.method, current.confidence
        ):
            return
    # GREATEST keeps the pointer monotonic if two appends commit out of order.
    db.execute(
        """
        WITH appended AS (
            INSERT INTO application_matches
                (message_id, application_id, method, confidence, rationale, actor_user_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, message_id
        )
        UPDATE email_messages m
        SET current_match_id = GREATEST(m.current_match_id, a.id)
        FROM appended a WHERE m.id = a.message_id
        """,
        (
            message_id,
            match.application_id,
            match.method,
            match.confidence,
            match.rationale,
            actor_user_id,
        ),
    )


def latest(message_id: int) -> CurrentMatch | None:
    """Return the newest match row for `message_id`, or None."""
    return db.query_one_as(
        CurrentMatch,
        "SELECT id, message_id, application_id, method, confidence, rationale, "
        "actor_user_id, created_at FROM application_matches "
        "WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )


def confirm(message_id: int, current: dict, *, actor_user_id: int, note: str | None = None) -> None:
    """Append a human confirmation preserving the original method and confidence
    so tier accuracy remains measurable."""
    record(
        message_id,
        MatchDecision(
            current["application_id"],
            current["method"],
            current["confidence"],
            note or "confirmed by a person",
        ),
        actor_user_id=actor_user_id,
    )


def reject(message_id: int, *, actor_user_id: int, note: str | None = None) -> None:
    """Append a human detachment (`application_id=None`, `method=detached`) so
    the message returns to the review queue without the matcher overriding it."""
    record(
        message_id,
        MatchDecision.detached(note or "rejected by a person"),
        actor_user_id=actor_user_id,
    )
