"""Identify a mailbox's own addresses and correct self-sent events."""

from __future__ import annotations

from api import db
from api.mail import events as mail_events
from api.mail.current import current_event

# Mail the candidate SENT is not an inbound event about the candidate. The
# instruction asks the model what "the SENDER is communicating", and when the
# sender is the candidate every kind comes out backwards - his own reply to a
# professor saying he had started work and was enjoying it was classified as an
# offer. 942 messages are self-sent and 137 of them carried inbound events,
# inflating the interviewing and offer counts directly.
#
# Direction is knowable from the header, so it is read rather than inferred.
# Handing it to a model turns a fact into a probability.
#
# THE MESSAGES ARE KEPT, only their classification as inbound events is
# skipped. Sent mail is the best record that exists of WHEN the candidate
# applied - better than applications.applied_at, which currently infers it from
# the first inbound reply - and that is a different signal with a different
# shape, to be built deliberately rather than fall out of a misread event kind.
# Tested against every address the mailbox owns, not just users.email. That
# column holds one address; this person has two, and the second is the sender
# of 1,840 messages the guard never saw. core.identity derives the set from the
# corpus rather than taking a configured list - see there for why.
#
# The second clause is the OLM export's sent items. 252 messages carry no
# from_email at all, every one of them from that source. A message with no
# sender that IS addressed to someone else is one you sent: if it had arrived,
# you would be on it.
#
# It must have recipients, though, and that is not a detail. 184 of those 252
# have no recipients either - they are parse failures, and a message with
# neither end recorded says nothing about its direction. An earlier version of
# this clause claimed all 241 non-owner-addressed ones and was caught by
# another session's fixtures, which are exactly that shape. Requiring
# recipients leaves 57, which is the sent-items population.
_SELF_SENT = """(
        (COALESCE(m.from_email, '') <> '' AND EXISTS (
            SELECT 1 FROM unnest(%(identities)s::text[]) ident
            WHERE position(ident IN lower(m.from_email)) > 0))
        OR (COALESCE(m.from_email, '') = '' AND cardinality(m.to_emails) > 0
            AND NOT EXISTS (
                SELECT 1 FROM unnest(m.to_emails) recipient
                WHERE lower(recipient) = ANY(%(identities)s::text[])))
    )"""


def identities_for(user_id: int) -> list[str]:
    """Every address this mailbox owns, derived from the mailbox.

    Falls back to the user's configured address when the corpus is too small
    to have the property core.identity relies on - a mailbox of forty messages
    says nothing about who owns it, and an empty set there would switch the
    guard off entirely rather than merely leaving it as narrow as it was.

    There was a confirmed set that overrode this, written by a settings screen
    asking the user which addresses were theirs. Both are gone (#301): the
    columns were NULL in production, so the override branch had never once been
    taken and every correction this guard has ever made - 1,028 of them - came
    from the derivation below. A screen that has produced no data is not a
    safety net, and keeping the branch implied a fallback that was in fact the
    only path.
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
    configured = db.query_one("SELECT lower(email) AS email FROM users WHERE id = %s", (user_id,))
    return [(configured or {}).get("email") or ""]


def _heal_self_sent(identities: list[str]) -> int:
    """Supersede events already booked on mail the owner sent.

    The sweep only ever looks at messages with no events, so widening the
    identity set stops NEW errors and leaves the existing ones standing - 1,239
    consumed events on this corpus, including 440 interviews the owner
    scheduled for other people while hiring for HackPSU.

    Written as a correction rather than a deletion, for the reason the whole
    events table is append-only: latest-wins means one INSERT supersedes the
    old answer and the old answer stays readable, so what was retracted and why
    is still there to review.

    Self-healing rather than a one-off backfill, because the identity set is
    derived and can change - a new address appearing in the mailbox should
    correct its own history on the next sweep without anyone running anything.
    Costs nothing: no model is called, and a message already carrying the
    correction is excluded so this converges instead of rewriting forever.
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
        {"identities": identities},
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
