"""The parts more than one of these surfaces needs, spelled once.

Two reasons a thing is here rather than beside its route. The administrator
and the owner do the same job over different mailboxes - correcting what a
message is, listing what it could belong to - and a second copy of either
would drift from the first. And evidence is attached wherever a match or a
proposal is shown, which is two surfaces that are otherwise unrelated.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from api import db
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.routers import resolve
from core.answers import EVENT_KINDS


class Reclassification(BaseModel):
    kind: str
    company: str | None = None
    role_title: str | None = None
    note: str | None = None


def _resync_applications(message_id: int) -> list[int]:
    """Resync every application this message feeds, and say which they were.

    A changed kind can change what an application is waiting for, and it can
    change an application the person is not currently looking at - so the ids
    come back rather than an `ok`. Both the correction and the revert need
    this, which is why it is one function.
    """
    affected = [
        r["application_id"]
        for r in db.query(
            """
            SELECT DISTINCT application_id FROM application_matches
            WHERE message_id = %s AND application_id IS NOT NULL
            """,
            (message_id,),
        )
    ]
    for application_id in affected:
        mail_pipeline.sync_action_items(application_id)
    return affected


def _apply_classification(
    message: dict[str, Any], body: Reclassification, *, actor_user_id: int
) -> dict[str, Any]:
    """Append a corrected classification, recording WHO corrected it.

    `actor_user_id` is not always the message's owner: an administrator
    correcting somebody else's mailbox writes their own id here, and whether
    that was a self-correction is derived by comparing the two rather than
    stored a second time.
    """
    message_id = message["id"]
    if body.kind not in EVENT_KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {sorted(EVENT_KINDS)}")
    current = db.query_one(
        "SELECT detail FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    # Carry the extraction forward unless the correction replaces it. A person
    # fixing "acknowledgement" to "rejection" is not also asserting the company
    # was wrong, and blanking it would break the matching that already worked.
    detail = dict((current or {}).get("detail") or {})
    if body.company is not None:
        detail["company"] = body.company
    if body.role_title is not None:
        detail["role_title"] = body.role_title
    if body.note:
        detail["corrected_note"] = body.note
    detail["corrected_by_user"] = True

    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model, actor_user_id) "
        "VALUES (%s, %s, 'high', %s, NULL, %s)",
        (message_id, body.kind, db.jsonb(detail), actor_user_id),
    )
    affected = _resync_applications(message_id)
    return {
        "ok": True,
        "message_id": message["id"],
        "kind": body.kind,
        "affected_application_ids": affected,
    }


def _apply_revert(message_id: int, *, actor_user_id: int) -> dict[str, Any]:
    """Restore the model's last answer, recording who asked for the restore."""
    model_answer = db.query_one(
        "SELECT kind, confidence, detail, model FROM email_events "
        "WHERE message_id = %s AND model IS NOT NULL ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    if model_answer is None:
        raise HTTPException(status_code=409, detail="no model classification to restore")
    current = db.query_one(
        "SELECT model FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    if current and current["model"] is not None:
        return {"ok": True, "already": True, "kind": model_answer["kind"]}

    detail = dict(model_answer["detail"] or {})
    detail.pop("corrected_by_user", None)
    detail.pop("corrected_note", None)
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence, detail, model, actor_user_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            message_id,
            model_answer["kind"],
            model_answer["confidence"],
            db.jsonb(detail),
            model_answer["model"],
            actor_user_id,
        ),
    )
    # The same affected ids classify returns. A revert moves the derived stage
    # exactly as a correction does, and possibly on an application nobody is
    # looking at, so it cannot be an {ok: true}.
    return {
        "ok": True,
        "already": False,
        "kind": model_answer["kind"],
        "affected_application_ids": _resync_applications(message_id),
    }


def _candidates_payload(
    message: dict[str, Any], owner_id: int, q: str | None, limit: int
) -> dict[str, Any]:
    """Candidates for a message, scoped to the message's OWNER.

    Separated from the route because the admin view asks the same question
    about somebody else's mailbox. The owner is a parameter rather than the
    caller precisely so that difference is stated once instead of a second
    copy of the ranking drifting from this one.
    """
    message_id = message["id"]
    event = db.query_one(
        "SELECT detail FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    detail = (event or {}).get("detail") or {}
    company = detail.get("company")
    key = mail_match.norm_company(company)

    apps = db.query(
        """
        SELECT a.id, a.job_id, a.company_name, a.title, a.applied_at,
               a.source_provenance, a.dismissed_at, uj.status AS board_status
        FROM applications a
        LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
        WHERE a.user_id = %s
        """,
        (owner_id,),
    )
    events = mail_pipeline.events_by_application(owner_id)
    needle = (q or "").lower().strip()
    scored = []
    for app in apps:
        haystack = f"{app['company_name'] or ''} {app['title'] or ''}".lower()
        if needle and needle not in haystack:
            continue
        same_company = bool(key) and mail_match.norm_company(app["company_name"]) == key
        own = events.get(app["id"], [])
        scored.append(
            {
                **app,
                "stage": mail_pipeline.stage_for(own, app["board_status"]),
                "event_count": len(own),
                # Why it is on the list at all. A candidate the matcher
                # considered and declined to choose between is a different
                # thing from a search hit, and the UI should be able to say so.
                "reason": "same company as this mail" if same_company else "search match",
                "_rank": (0 if same_company else 1, app["company_name"] or ""),
            }
        )
    scored.sort(key=lambda r: r["_rank"])
    ambiguous = sum(1 for r in scored if r["reason"] == "same company as this mail")

    jobs = db.query(
        """
        SELECT j.id, j.company, j.title, j.url, uj.date_applied, uj.status
        FROM user_jobs uj JOIN jobs j ON j.id = uj.job_id
        WHERE uj.user_id = %(user)s
          AND NOT EXISTS (
              SELECT 1 FROM applications a WHERE a.user_id = uj.user_id AND a.job_id = uj.job_id
          )
          AND (%(q)s::text IS NULL OR lower(j.company) LIKE %(like)s OR lower(j.title) LIKE %(like)s)
        ORDER BY uj.date_applied DESC NULLS LAST
        LIMIT %(limit)s
        """,
        {"user": owner_id, "q": q, "like": f"%{needle}%", "limit": limit},
    )
    return {
        "message": {
            "id": message["id"],
            "subject": message["subject"],
            "from_email": message["from_email"],
            "sent_at": message["sent_at"],
            "extracted_company": company,
            "extracted_title": detail.get("role_title"),
        },
        "applications": [
            {k: v for k, v in row.items() if not k.startswith("_")} for row in scored[:limit]
        ],
        # The verbs available here, decided by the server. The modal is the
        # surface a person actually makes this decision on, and it is reached
        # from the mail list and the unmatched queue rather than only from a
        # queue page - so it needs the same declared choices the queue rows
        # carry, or its eligibility is a client-side guess.
        "choices": resolve.choices_for_message(
            # Only the undismissed ones decide eligibility. This list carries
            # dismissed applications so the picker can show them; assigning to
            # one is refused at the write, so they must not make "belongs to an
            # application" look available.
            resolve.by_company([a for a in apps if a["dismissed_at"] is None]),
            company,
            resolve.thread_size(owner_id, message.get("provider_thread_id")),
            # This payload calls its list `applications`, not `candidates`. A
            # client reads `payload[choice.target_source]`, so naming the
            # queue's key here would point it at a field this response does not
            # have - the hardcoded fact moved rather than removed.
            resolve.PICKER_APPLICATIONS,
        ),
        "total_applications": len(scored),
        # The count the matcher choked on. Two or more means it refused on
        # purpose rather than finding nothing.
        "same_company_candidates": ambiguous,
        "board_jobs": jobs,
    }


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


def _mention(body: str | None, needle: str | None) -> Mention | None:
    """The whole message, and WHERE the company is mentioned in it.

    Not an excerpt. An excerpt meant reading a fragment, clicking, and then
    reading the same message again from the top with no marker on the part that
    mattered - the reader did the finding twice and we helped neither time.

    Offsets rather than a highlighted string, so the client decides how to mark
    it and the body stays exactly what the sender wrote. `start` is null when
    the company does not appear verbatim, which is common: the classifier reads
    it off a signature or a logo as often as out of a sentence.

    The body is already capped at MAX_BODY_CHARS on the way in, so this cannot
    be unbounded no matter how long the original was.
    """
    text = (body or "").strip()
    if not text:
        return None
    term = (needle or "").strip()
    found = text.lower().find(term.lower()) if term else -1
    return Mention(
        text=text,
        start=found if found >= 0 else None,
        end=found + len(term) if found >= 0 else None,
        term=term if found >= 0 else None,
    )


def _evidence_for(message_ids: list[int]) -> dict[int, Evidence]:
    """What each match actually rested on.

    The rationale field says what the matcher concluded; this says what it
    concluded it FROM. A person checking a match needs the second one - the
    company the classifier read out of the mail is the thing tier 2 and 3
    compared, so if that extraction is wrong the match is wrong and no amount
    of staring at the conclusion reveals it.
    """
    if not message_ids:
        return {}
    rows = db.query(
        """
        WITH current_event AS (
            SELECT DISTINCT ON (message_id) message_id, kind, confidence, detail, model
            FROM email_events ORDER BY message_id, id DESC
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
            # What the classifier read out of the mail. Tier 2 and 3 compare
            # THIS, not the raw text, so a wrong extraction is a wrong match.
            extracted_company=company,
            extracted_title=detail.get("role_title"),
            classified_as=row["kind"],
            classifier_confidence=row["confidence"],
            classifier_model=row["model"],
            # The sender is the one fact no model produced. An ATS domain is
            # near-proof of a real application; a .edu sender usually is not.
            from_domain=(row["from_email"] or "").split("@")[-1].lower() or None,
            mention=_mention(row["body_text"], company),
            body_chars=len(row["body_text"] or ""),
        )
    return out
