"""Tying an email to the application it is about.

Four tiers, cheapest and most certain first. Anything the tiers cannot settle
stays UNMATCHED and is recorded as such - a wrong match is worse than no
match, because everything downstream treats a match as fact.

Two properties make this affordable and safe:

The candidate set is small. Matching is against the user's ~716 applications,
not the 49k job catalog, so even an exhaustive comparison is cheap.

Matching RE-RUNS. `application_matches` is append-only, so a message that
could not be matched in March can match in September when the posting finally
appears on the board. Nothing here is a one-shot decision, which is what makes
"applied first, added to the board later" work by recomputation rather than
repair.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from api import db
from core.ats import canonicalize

logger = logging.getLogger("jobtracker_worker")

# Tiers, most certain first. Stored on the row so the debug view can show
# which one fired - a matcher nobody can audit is a matcher nobody can fix.
EXACT_LINK = "exact_link"
ATS_COMPANY = "ats_company"
COMPANY_TITLE = "company_title"
ADJUDICATED = "adjudicated"
UNMATCHED = "unmatched"

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# How long after applying a message may still plausibly concern that
# application. Rejections routinely arrive months later, and an ATS
# occasionally mails about a role a year on, so this is deliberately generous:
# the window exists to stop a 2019 application absorbing a 2026 email, not to
# be a precise claim about hiring timelines.
MATCH_WINDOW_DAYS = 400


@dataclass(frozen=True)
class Match:
    application_id: int | None
    method: str
    confidence: str
    rationale: str


def urls_in(body: str | None) -> list[str]:
    return _URL_RE.findall(body or "")


def canonical_urls(body: str | None) -> set[str]:
    """Canonical posting URLs mentioned anywhere in the message.

    ATS mail almost always links back to the posting or the application, and
    core.ats already knows how to reduce those links to a stable identity -
    the same function that dedupes the job catalog. Reusing it means a link in
    an email and the job row it points at agree by construction rather than by
    a second, parallel spelling.
    """
    found = set()
    for url in urls_in(body):
        canonical = canonicalize(url.rstrip(".,);"))
        if canonical:
            found.add(canonical)
    return found


def _by_exact_link(user_id: int, body: str | None) -> Match | None:
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
            return Match(
                row["id"],
                EXACT_LINK,
                "high",
                f"message links to {job_canonical}",
            )
    return None


def _norm_company(value: str | None) -> str:
    """Loose enough that 'Stripe' and 'Stripe, Inc.' agree.

    company is free text on both sides, so exact equality would treat those as
    different employers - the same weakness that makes the reposted-role count
    an undercount elsewhere in this codebase.
    """
    text = (value or "").lower().strip()
    text = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|sa|nv)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def _by_company(user_id: int, company: str | None, sent_at) -> Match | None:
    """One application at that company inside the window, or nothing.

    Ambiguity is not resolved here on purpose. Two open applications at the
    same employer is exactly the case a human or a model should settle, and
    guessing would produce a confident wrong answer that nothing downstream
    can question.
    """
    key = _norm_company(company)
    if not key:
        return None
    rows = db.query(
        """
        SELECT a.id, a.company_name, a.title, a.applied_at
        FROM applications a
        WHERE a.user_id = %s
          AND (a.applied_at IS NULL OR %s::timestamptz IS NULL
               OR a.applied_at <= %s::timestamptz)
        """,
        (user_id, sent_at, sent_at),
    )
    candidates = [r for r in rows if _norm_company(r["company_name"]) == key]
    if len(candidates) == 1:
        return Match(
            candidates[0]["id"],
            ATS_COMPANY,
            "medium",
            f"single application at {candidates[0]['company_name']}",
        )
    if len(candidates) > 1:
        logger.debug(f"{len(candidates)} candidates at {company} for user {user_id}")
    return None


def _by_company_and_title(user_id: int, company: str | None, title: str | None) -> Match | None:
    key, role = _norm_company(company), _norm_company(title)
    if not key or not role:
        return None
    rows = db.query(
        "SELECT id, company_name, title FROM applications WHERE user_id = %s", (user_id,)
    )
    candidates = [
        r
        for r in rows
        if _norm_company(r["company_name"]) == key and _norm_company(r["title"]) == role
    ]
    if len(candidates) == 1:
        return Match(
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
) -> Match:
    """Best available match, or an explicit non-match.

    Never returns a guess. UNMATCHED with a NULL application_id is a real
    recorded outcome meaning "we looked and found nothing", which is different
    from never having looked and is what lets a later re-run improve on it.
    """
    for finder in (
        lambda: _by_exact_link(user_id, body),
        lambda: _by_company(user_id, company, sent_at),
        lambda: _by_company_and_title(user_id, company, title),
    ):
        found = finder()
        if found is not None:
            return found
    return Match(None, UNMATCHED, "none", "no candidate matched")


def record(message_id: int, match: Match) -> None:
    db.execute(
        """
        INSERT INTO application_matches (message_id, application_id, method, confidence, rationale)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (message_id, match.application_id, match.method, match.confidence, match.rationale),
    )


def latest(message_id: int) -> dict | None:
    """The current match. Append-only, so the newest row wins - the same rule
    as the verdict log, and what makes re-running non-destructive."""
    return db.query_one(
        "SELECT * FROM application_matches WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
