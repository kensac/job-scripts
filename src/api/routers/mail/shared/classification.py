"""Correct what a message is, or restore the model's answer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from api import db
from api.mail import events as mail_events
from api.problem import refuse
from core.answers import EVENT_KINDS


class Reclassification(BaseModel):
    kind: str
    company: str | None = None
    role_title: str | None = None
    note: str | None = None


class Reclassified(BaseModel):
    """What a correction moved. The affected applications come back rather
    than an `ok`, because a changed kind can change what an application is
    waiting for - including one the person is not currently looking at."""

    ok: bool
    message_id: int
    kind: str
    affected_application_ids: list[int]


class Reverted(BaseModel):
    """What restoring the model's answer moved.

    `already` says the model's answer was already in force, and then nothing
    was resynced - so `affected_application_ids` is absent rather than empty,
    and the routes carry `response_model_exclude_none` to keep it that way. An
    empty list would claim a recomputation that did not run."""

    ok: bool
    already: bool
    kind: str
    affected_application_ids: list[int] | None = None


def _resync_applications(message_id: int) -> list[int]:
    """Every application this message feeds, which a changed kind changes.

    A changed kind can change what an application is waiting for, and it can
    change an application the person is not currently looking at - so the ids
    come back rather than an `ok`. Its asks are read from the events, so
    nothing needs recomputing.
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
    return affected


def apply_classification(
    message: dict[str, Any], body: Reclassification, *, actor_user_id: int
) -> Reclassified:
    """Append a corrected classification, recording WHO corrected it.

    `actor_user_id` is not always the message's owner: an administrator
    correcting somebody else's mailbox writes their own id here, and whether
    that was a self-correction is derived by comparing the two rather than
    stored a second time.
    """
    message_id = message["id"]
    if body.kind not in EVENT_KINDS:
        raise refuse(400, "INVALID_KIND", f"kind must be one of {sorted(EVENT_KINDS)}")
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

    mail_events.append(
        message_id, body.kind, confidence="high", detail=detail, actor_user_id=actor_user_id
    )
    affected = _resync_applications(message_id)
    return Reclassified(
        ok=True,
        message_id=message["id"],
        kind=body.kind,
        affected_application_ids=affected,
    )


def apply_revert(message_id: int, *, actor_user_id: int) -> Reverted:
    """Restore the model's last answer, recording who asked for the restore."""
    model_answer = db.query_one(
        "SELECT kind, confidence, detail, model FROM email_events "
        "WHERE message_id = %s AND model IS NOT NULL ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    if model_answer is None:
        raise refuse(409, "NO_MODEL_ANSWER", "no model classification to restore")
    current = db.query_one(
        "SELECT model FROM email_events WHERE message_id = %s ORDER BY id DESC LIMIT 1",
        (message_id,),
    )
    if current and current["model"] is not None:
        return Reverted(ok=True, already=True, kind=model_answer["kind"])

    detail = dict(model_answer["detail"] or {})
    detail.pop("corrected_by_user", None)
    detail.pop("corrected_note", None)
    mail_events.append(
        message_id,
        model_answer["kind"],
        confidence=model_answer["confidence"],
        detail=detail,
        model=model_answer["model"],
        actor_user_id=actor_user_id,
    )
    # The same affected ids classify returns. A revert moves the derived stage
    # exactly as a correction does, and possibly on an application nobody is
    # looking at, so it cannot be an {ok: true}.
    return Reverted(
        ok=True,
        already=False,
        kind=model_answer["kind"],
        affected_application_ids=_resync_applications(message_id),
    )
