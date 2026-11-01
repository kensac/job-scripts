"""What a match or a proposal rested on."""

from __future__ import annotations

from pydantic import BaseModel

from api import db
from api.mail.current import current_event


class Mention(BaseModel):
    """The whole message, and where the company appears in it.

    Offsets rather than a highlighted string, so the client decides how to
    mark it and the body stays exactly what the sender wrote. `start` is null
    when the company does not appear verbatim, which is common: the classifier
    reads it off a signature or a logo as often as out of a sentence."""

    text: str
    start: int | None
    end: int | None
    term: str | None


class Evidence(BaseModel):
    """What a match actually rested on.

    The rationale says what the matcher concluded; this says what it concluded
    it FROM. The company the classifier read out of the mail is what tiers 2
    and 3 compared, so if that extraction is wrong the match is wrong and no
    amount of staring at the conclusion reveals it."""

    extracted_company: str | None
    extracted_title: str | None
    classified_as: str | None
    classifier_confidence: str | None
    classifier_model: str | None
    # The one fact no model produced. An ATS domain is near proof of a real
    # application; a .edu sender usually is not.
    from_domain: str | None
    mention: Mention | None
    body_chars: int


# What the stored body was capped at until bodies were kept whole, so the
# evidence response stays the size it was. body_chars still says the length.
_MENTION_CHARS = 20_000


def _mention(body: str | None, needle: str | None) -> Mention | None:
    """The whole message, and WHERE the company is mentioned in it.

    Not an excerpt. An excerpt meant reading a fragment, clicking, and then
    reading the same message again from the top with no marker on the part that
    mattered - the reader did the finding twice and we helped neither time.

    Bounded at _MENTION_CHARS: the body is stored whole, and this goes out once
    per piece of evidence in a list.
    """
    text = (body or "").strip()[:_MENTION_CHARS]
    if not text:
        return None
    term = (needle or "").strip()
    found = text.lower().find(term.lower()) if term else -1
    if found < 0:
        return Mention(text=text, start=None, end=None, term=None)
    return Mention(text=text, start=found, end=found + len(term), term=term)


def evidence_for(message_ids: list[int]) -> dict[int, Evidence]:
    """The Evidence for each message, keyed by message id."""
    if not message_ids:
        return {}
    rows = db.query(
        f"""
        WITH current_event AS (
            {current_event("kind", "confidence", "detail", "model")}
        )
        SELECT m.id, m.from_email, m.subject, m.sent_at, m.body_text,
               e.kind, e.confidence, e.detail, e.model
        FROM email_messages m
        LEFT JOIN current_event e ON e.message_id = m.id
        WHERE m.id = ANY(%s)
        """,
        (message_ids,),
    )
    out: dict[int, Evidence] = {}
    for row in rows:
        detail = row["detail"] or {}
        company = detail.get("company")
        out[row["id"]] = Evidence(
            extracted_company=company,
            extracted_title=detail.get("role_title"),
            classified_as=row["kind"],
            classifier_confidence=row["confidence"],
            classifier_model=row["model"],
            from_domain=(row["from_email"] or "").split("@")[-1].lower() or None,
            mention=_mention(row["body_text"], company),
            body_chars=len(row["body_text"] or ""),
        )
    return out
