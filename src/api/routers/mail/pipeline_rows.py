"""Person-owned applications and their derived mail evidence."""

from __future__ import annotations

import datetime
from typing import NamedTuple

from pydantic import BaseModel

from api import db
from api.mail import match as mail_match
from api.mail import pipeline as mail_pipeline
from api.mail.current import current_match
from api.mail.pipeline import SenderSignal


def correction_actor_label(
    actor_user_id: int | None, viewer_id: int, model: str | None = None
) -> str:
    """Who decided this row, from the viewer's point of view.

    FOUR ANSWERS, not three, and the fourth is the honest one. A machine wrote
    it; the viewer wrote it; an administrator wrote it; or a person wrote it
    before this column existed and there is no record of which person. That
    last case is not the same as "nobody corrected it" and must not be rendered
    as if it were - every human correction made before actor_user_id was added
    lands there, and the logs are append-only so it can never be resolved.

    The admin's corrections are surfaced to the affected user rather than
    hidden: a correction someone cannot see is one they cannot question, and
    finding your own data changed with no account of who changed it is worse
    than the change being visible.
    """
    if model is not None:
        return "model"
    if actor_user_id is None:
        return "unknown"
    return "you" if actor_user_id == viewer_id else "administrator"


# Strongest evidence first. A tier is a property of a MATCH, not of an
# application - matches are append-only and a message can be rematched - so a
# row reports the strongest tier among its current matches and says so.
_TIER_STRENGTH = (
    mail_match.EXACT_LINK,
    mail_match.ATS_COMPANY,
    mail_match.COMPANY_TITLE,
    mail_match.ADJUDICATED,
    "derived",
)


class _Tier(NamedTuple):
    """The strongest match behind an application, and how sure that match was.
    Not a response shape: the two fields are spelled on the row itself, where
    a reader can see which tier `tier_confidence` belongs to."""

    method: str
    confidence: str | None


def strongest_tiers_by_application(user_id: int) -> dict[int, _Tier]:
    rows = db.query(
        f"""
        WITH current_match AS (
            {current_match("application_id", "method", "confidence")}
        )
        SELECT cm.application_id, cm.method, cm.confidence
        FROM current_match cm JOIN applications a ON a.id = cm.application_id
        WHERE a.user_id = %s AND cm.application_id IS NOT NULL
        """,
        (user_id,),
    )
    best: dict[int, tuple[int, str, str | None]] = {}
    for row in rows:
        app_id = row["application_id"]
        rank = _TIER_STRENGTH.index(row["method"]) if row["method"] in _TIER_STRENGTH else 99
        if app_id not in best or rank < best[app_id][0]:
            best[app_id] = (rank, row["method"], row["confidence"])
    return {app_id: _Tier(method, confidence) for app_id, (_, method, confidence) in best.items()}


class ApplicationRow(BaseModel):
    """The application as stored, with the board status beside it.

    `board_status` is the LEFT JOIN: 1,817 of 2,543 applications here have no
    board row, because mail predating the catalog is the normal case, and null
    means there is no posting to disagree with rather than a posting with no
    status."""

    id: int
    job_id: int | None
    company_name: str | None
    title: str | None
    applied_at: datetime.datetime | None
    source_provenance: str
    dismissed_at: datetime.datetime | None
    dismissed_reason: str | None
    board_status: str | None


class PipelineApplication(ApplicationRow):
    """The row plus everything derived from the mail about it.

    Nothing here is stored. `stage` is recomputed from the events on every
    read, which is what makes a late or reclassified message correct itself
    rather than need repairing, and `strongest_tier` is a property of the
    MATCHES rather than of the application - matches are append-only, so a row
    reports the strongest tier among its current ones."""

    stage: str
    event_count: int
    last_event_at: datetime.datetime | None
    strongest_tier: str | None
    tier_confidence: str | None
    # A possibility, not a verdict: nothing filters on this and no application
    # is hidden by it. It says "worth confirming", and carries the evidence so
    # the reader can disagree.
    sender: SenderSignal | None


def application_rows_for_user(user_id: int) -> list[PipelineApplication]:
    """Every application with its derived stage. One query for applications,
    one for all their events - not two per application."""
    apps = db.query_as(
        ApplicationRow,
        """
        SELECT a.id, a.job_id, a.company_name, a.title, a.applied_at,
               a.source_provenance, a.dismissed_at, a.dismissed_reason,
               uj.status AS board_status
        FROM applications a
        LEFT JOIN user_jobs uj ON uj.job_id = a.job_id AND uj.user_id = a.user_id
        WHERE a.user_id = %s
        """,
        (user_id,),
    )
    events = mail_pipeline.events_by_application(user_id)
    tiers = strongest_tiers_by_application(user_id)
    # Derived at read time and attached rather than stored, so it moves when
    # core.fetching.ats learns a provider or when a sender turns out to serve more
    # companies than it did when the match was made.
    senders = mail_pipeline.sender_signal(user_id)
    application_rows = []
    for app in apps:
        application_events = events.get(app.id, [])
        strongest_tier = tiers.get(app.id)
        application_rows.append(
            PipelineApplication(
                **app.model_dump(),
                stage=mail_pipeline.stage_for(application_events, app.board_status),
                event_count=len(application_events),
                last_event_at=max(
                    (e.sent_at for e in application_events if e.sent_at), default=None
                ),
                strongest_tier=strongest_tier.method if strongest_tier else None,
                tier_confidence=strongest_tier.confidence if strongest_tier else None,
                sender=senders.get(app.id),
            )
        )
    return application_rows
