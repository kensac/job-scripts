"""Identify a mailbox's own addresses and correct self-sent events."""

from __future__ import annotations

from api import db
from api.mail import events as mail_events
from api.mail.current import current_event

# Sent mail is not an inbound event. A historical corpus audit found 942
# self-sent messages; 137 had been classified as inbound events. Keep these
# messages because they can still provide application evidence.
#
# Check all mailbox identities, not only users.email. The second address in
# the audited mailbox sent 1,840 messages. core.identity derives the set.
#
# OLM sent items can lack from_email. Of 252 such messages, 184 also lack
# recipients and give no direction evidence. Require a recipient before
# treating a message with no sender as sent; 57 met that condition in the audit.
#
# Binds `identities` as the jsonb map identities_by_owner returns: each
# message is judged by its own owner's addresses.
_OWNED = "ARRAY(SELECT jsonb_array_elements_text(%(identities)s::jsonb -> m.user_id::text))"
_SELF_SENT = f"""(
        (COALESCE(m.from_email, '') <> '' AND EXISTS (
            SELECT 1 FROM unnest({_OWNED}) ident
            WHERE position(ident IN lower(m.from_email)) > 0))
        OR (COALESCE(m.from_email, '') = '' AND cardinality(m.to_emails) > 0
            AND NOT EXISTS (
                SELECT 1 FROM unnest(m.to_emails) recipient
                WHERE lower(recipient) = ANY({_OWNED})))
    )"""


def identities_for(user_id: int) -> list[str]:
    """Every address this mailbox owns, derived from the mailbox.

    When the mailbox is too small for core.identity to infer ownership, use
    the user's configured address. This keeps the sent-mail guard active.

    A former settings override had no production values when audited for
    #301. All 1,028 recorded corrections used derived identities.
    """
    from core.identity import AddressCount, derive_identities

    rows = db.query(
        """
        WITH addrs AS (
            SELECT lower(a) AS addr, m.id FROM email_messages m,
                 LATERAL unnest(m.to_emails) a WHERE m.user_id = %(uid)s
            UNION
            SELECT lower(m.from_email), m.id FROM email_messages m
            WHERE m.user_id = %(uid)s AND COALESCE(m.from_email, '') <> ''
        )
        SELECT addr, COUNT(DISTINCT id) AS messages
        FROM addrs WHERE addr LIKE '%%@%%' GROUP BY addr ORDER BY 2 DESC LIMIT 200
        """,
        {"uid": user_id},
    )
    total = db.query_one("SELECT COUNT(*) AS n FROM email_messages WHERE user_id = %s", (user_id,))
    found = derive_identities(
        [AddressCount(r["addr"], r["messages"]) for r in rows], (total or {}).get("n") or 0
    )
    if found:
        return sorted(found)
    # No address at all is no identity. An empty string would match every
    # sender: position('' IN x) is 1.
    configured = db.query_one("SELECT lower(email) AS email FROM users WHERE id = %s", (user_id,))
    email = (configured or {}).get("email")
    return [email] if email else []


def identities_by_owner() -> dict[int, list[str]]:
    """Each mailbox owner's addresses, keyed by user id, for `_SELF_SENT`."""
    return {
        row["user_id"]: identities_for(row["user_id"])
        for row in db.query("SELECT DISTINCT user_id FROM email_messages ORDER BY user_id")
    }


def _heal_self_sent(identities: dict[int, list[str]]) -> int:
    """Supersede events already booked on mail the owner sent.

    The ordinary sweep selects messages with no events. An earlier corpus
    audit found 1,239 existing self-sent events, including 440 interviews the
    owner scheduled for others. New identity evidence must correct those too.

    Append a correction so the prior event remains reviewable. Repeat this
    check on each sweep because derived identities can change. Already
    corrected messages are excluded, and no model is called.
    """
    rows = db.query(
        f"""
        WITH latest AS (
            {current_event("kind", "detail")}
        )
        SELECT m.id FROM email_messages m JOIN latest l ON l.message_id = m.id
        WHERE {_SELF_SENT}
          AND NOT (l.kind = 'not_job_related' AND l.detail ->> 'reason' = 'self_sent')
        """,
        {"identities": db.jsonb(identities)},
    )
    for row in rows:
        mail_events.append(
            row["id"],
            "not_job_related",
            confidence="high",
            detail={"reason": "self_sent", "superseded": True},
            model=mail_events.SELF_SENT_RULE,
        )
    return len(rows)
