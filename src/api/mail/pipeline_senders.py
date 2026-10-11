"""Read-time sender evidence for mail-derived applications."""

from __future__ import annotations

from pydantic import BaseModel

from api import db
from api.mail.current import current_match
from core.fetching import ats


class SenderSignal(BaseModel):
    """What an application's first sender says about whether this is an
    employer relationship at all.

    A possibility, never a verdict: nothing filters or hides on this.
    `sender_company_count` is the evidence and `review_suggested` is only a
    reading of it, so both travel and the reader can disagree."""

    sender_domain: str | None
    sender_is_ats: bool
    sender_company_count: int
    review_suggested: bool
    why: str


# A non-ATS sender domain that has produced this many DISTINCT company names is
# an intermediary rather than one employer's own mail server. Derived, not
# picked: bucketing every mail-derived application by its sender's spread puts
# the junk rate at 4% for domains naming one company and 9% for two - the base
# rate - then 41% at three to four and 32% at five or more. The jump sits
# between two and three.
INTERMEDIARY_COMPANY_NAMES = 3


def sender_signal(user_id: int) -> dict[int, SenderSignal]:
    """Per application, what its FIRST message's sender says about whether this
    is an employer relationship at all.

    Computed at read time and deliberately not stored. The ATS half is a
    property of the sender domain, so it has to move when core.fetching.ats learns a new
    provider; the spread half is a function of the user's whole message
    history, so a domain that becomes an intermediary after a match was made
    has to change the answer for matches already written. Freezing either at
    match time would preserve the wrong one.

    A POSSIBILITY, NEVER A VERDICT. Nothing here drops or hides an application.
    The strongest thing it says is "worth a look", because the failure this
    exists to catch - an organisation the user has a relationship with that is
    not an employer he applied to - is also the shape of a perfectly real
    application through a job board.

    What it does NOT do is guess from the company name. Blocking universities
    breaks applying to a university for a job, which is normal; the three
    organisations that motivated this were caught by three different special
    cases, and a fourth would have needed a fifth.
    """
    rows = db.query(
        f"""
        WITH first_message AS (
            SELECT DISTINCT ON (am.application_id) am.application_id, m.from_email
            FROM ({current_match("application_id")}) am
            JOIN email_messages m ON m.id = am.message_id
            JOIN applications a ON a.id = am.application_id
            WHERE am.application_id IS NOT NULL AND a.user_id = %(user_id)s
            ORDER BY am.application_id, m.sent_at ASC
        ),
        with_domain AS (
            SELECT f.application_id,
                   lower(COALESCE(NULLIF(split_part(f.from_email, '@', 2), ''), '')) AS domain,
                   COALESCE(a.company_name, '') AS company
            FROM first_message f
            JOIN applications a ON a.id = f.application_id
        ),
        spread AS (
            SELECT domain, count(DISTINCT company) AS companies
            FROM with_domain WHERE domain <> '' GROUP BY domain
        )
        SELECT w.application_id, w.domain, COALESCE(s.companies, 0) AS companies
        FROM with_domain w LEFT JOIN spread s ON s.domain = w.domain
        """,
        {"user_id": user_id},
    )
    signals_by_application: dict[int, SenderSignal] = {}
    for row in rows:
        domain = row["domain"] or None
        companies = int(row["companies"] or 0)
        is_ats = ats.is_ats_email_domain(domain)
        shared = bool(domain) and not is_ats and companies >= INTERMEDIARY_COMPANY_NAMES
        if is_ats:
            why = "sent by an applicant-tracking system, which is near-proof of a real application"
        elif shared:
            why = (
                f"{domain} is the first sender for {companies} different companies, so it is a "
                "platform or an organisation you have a standing relationship with rather than "
                "one employer's own mail - worth confirming you applied here"
            )
        else:
            why = "sent from the employer's own domain"
        signals_by_application[row["application_id"]] = SenderSignal(
            sender_domain=domain,
            sender_is_ats=is_ats,
            # How many distinct companies this user has derived from that
            # sender. Exposed rather than reduced to a flag, because it is the
            # evidence and the flag is only a reading of it.
            sender_company_count=companies,
            review_suggested=shared,
            why=why,
        )
    return signals_by_application


def intermediary_domains() -> list[str]:
    """Sender domains that are platforms rather than one employer's own mail.

    The same rule as sender_signal, across every user rather than one, for the
    aggregate surfaces that have no user in scope. Defined once here so the
    threshold and the ATS exemption cannot drift between the per-application
    reading and the per-company one.
    """
    rows = db.query(
        f"""
        WITH first_message AS (
            SELECT DISTINCT ON (am.application_id) am.application_id, m.from_email
            FROM ({current_match("application_id")}) am
            JOIN email_messages m ON m.id = am.message_id
            WHERE am.application_id IS NOT NULL
            ORDER BY am.application_id, m.sent_at ASC
        )
        SELECT lower(COALESCE(NULLIF(split_part(f.from_email, '@', 2), ''), '')) AS domain,
               count(DISTINCT COALESCE(a.company_name, '')) AS companies
        FROM first_message f
        JOIN applications a ON a.id = f.application_id
        WHERE a.dismissed_at IS NULL
        GROUP BY 1
        """
    )
    return [
        r["domain"]
        for r in rows
        if r["domain"]
        and not ats.is_ats_email_domain(r["domain"])
        and int(r["companies"]) >= INTERMEDIARY_COMPANY_NAMES
    ]
