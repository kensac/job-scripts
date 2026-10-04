"""The one-pass resolve queue answers exactly what the four-statement queue did.

The fixture holds every case the rewrite could get wrong: a message whose kind
was reclassified, a match superseded by a rematch and one by a refusal, a
message with no event, another user's message matched to the owner's
application, a dismissed application, a board that already moved on, an
answered proposal, a person's own attachment, action items with and without an
application, a thread, a NULL `sent_at`, and ties on `sent_at` across kinds.
No two rows of one kind share a `sent_at`, because the old SQL left their
order to the plan (see tests/resolve_queue_oracle.py).
"""

from __future__ import annotations

import datetime

import pytest

from api import db
from api.mail import pipeline as mail_pipeline
from api.resolve import queue, queue_items
from tests import resolve_queue_oracle as oracle

_T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def _msg(uid, n, events, *, company="Acme", sent=None, thread=None):
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, provider_thread_id, source, "
        "from_email, subject, sent_at) VALUES (%s, %s, %s, 'gmail', %s, %s, %s) RETURNING id",
        (uid, f"<m{n}@x>", thread, f"hr{n}@x.test", f"subject {n}", sent),
    )
    ids = []
    for kind in events:
        ids.append(
            db.query_one(
                "INSERT INTO email_events (message_id, kind, confidence, detail, model) "
                "VALUES (%s, %s, 'high', %s, 'gpt-5-nano') RETURNING id",
                (row["id"], kind, db.jsonb({"company": company, "role_title": f"role {n}"})),
            )["id"]
        )
    return row["id"], ids


def _match(mid, app, method="ats_company", actor=None):
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method, confidence, "
        "rationale, actor_user_id) VALUES (%s, %s, %s, 'high', 'why', %s)",
        (mid, app, method, actor),
    )


def _app(uid, company, *, board=None, dismissed=False, f=None):
    job = None
    if board is not None:
        job = f.make_job(company=company)
        db.execute(
            "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, %s)",
            (uid, job, board),
        )
    return db.query_one(
        "INSERT INTO applications (user_id, job_id, company_name, title, source_provenance, "
        "dismissed_at) VALUES (%s, %s, %s, 'Engineer', 'email', %s) RETURNING id",
        (uid, job, company, _T0 if dismissed else None),
    )["id"]


@pytest.fixture
def world(f):
    me, other = f.make_user(), f.make_user()
    live = _app(me, "Acme", board="Application Submitted", f=f)
    twin = _app(me, "Acme Inc")
    globex = _app(me, "Globex")
    moved_on = _app(me, "Initech", board="Rejected", f=f)
    gone = _app(me, "Acme", dismissed=True)

    def at(hours):
        return _T0 + datetime.timedelta(hours=hours)

    # Reclassified: acknowledgement then rejection, attached by the matcher.
    m1, _ = _msg(me, 1, ["acknowledgement", "rejection"], sent=at(10))
    _match(m1, live)
    # Reclassified out of the queue: rejection then not job mail.
    _msg(me, 2, ["rejection", "not_job_related"], sent=at(9))
    # Unmatched, two candidates, in a three-message thread; ties m1 across kinds.
    _msg(me, 3, ["interview_invite"], sent=at(10), thread="T")
    _msg(me, 31, ["not_job_related"], sent=at(1), thread="T")
    _msg(me, 32, ["not_job_related"], sent=at(2), thread="T")
    _msg(me, 33, ["not_job_related"], sent=at(3), thread="U")
    # Unmatched with candidates and no thread: assign moves only itself.
    _msg(me, 15, ["rejection"], company="Globex", sent=at(1))
    # Rematched: first Globex, now the live application.
    m4, m4_events = _msg(me, 4, ["offer"], sent=at(8))
    _match(m4, globex)
    _match(m4, live)
    # Refused after a match: neither queued nor evidence.
    m5, _ = _msg(me, 5, ["rejection"], sent=at(7))
    _match(m5, globex)
    _match(m5, None, method="not_an_application", actor=me)
    # A person attached it: no confirmation asked, still a proposal. Ties m4.
    m6, _ = _msg(me, 6, ["rejection"], company="Globex", sent=at(8))
    _match(m6, globex, method="manual", actor=me)
    # Answered proposal: the event is silenced.
    m7, m7_events = _msg(me, 7, ["position_closed"], company="Globex", sent=at(6))
    _match(m7, globex)
    db.execute(
        "INSERT INTO suggestion_responses (user_id, application_id, event_id, "
        "suggested_status, response) VALUES (%s, %s, %s, 'No Longer Available', 'dismissed')",
        (me, globex, m7_events[0]),
    )
    # Nobody's company, no sent_at.
    _msg(me, 8, ["acknowledgement"], company="Nobody Corp")
    # A match with no event at all.
    m9, _ = _msg(me, 9, [], sent=at(5))
    _match(m9, twin)
    # Another user's message on my application.
    m10, _ = _msg(other, 10, ["interview_scheduled"], sent=at(4))
    _match(m10, twin)
    # The board already moved on: a match to confirm, no proposal.
    m11, _ = _msg(me, 11, ["rejection"], company="Initech", sent=at(3))
    _match(m11, moved_on)
    # A dismissed application asks nothing.
    m12, _ = _msg(me, 12, ["rejection"], sent=at(2))
    _match(m12, gone)
    # Another user's unmatched mail is not mine.
    _msg(other, 13, ["rejection"], sent=at(11))
    # Unmatched, refusal-only, newest of all.
    _msg(me, 14, ["assessment_invite"], company="Hooli", sent=at(12))
    db.execute(
        "INSERT INTO action_items (user_id, application_id, event_id, kind, due_at) "
        "VALUES (%s, %s, %s, 'respond_to_offer', %s), (%s, NULL, NULL, 'reply_to_recruiter', NULL)",
        (me, live, m4_events[0], at(30), me),
    )
    return me


_KINDS = [None, ["unmatched_message"], ["unconfirmed_match"], ["status_proposal", "action_item"]]


@pytest.mark.parametrize("kinds", _KINDS)
@pytest.mark.parametrize(("limit", "offset"), [(1, 0), (1, 3), (50, 0), (5, 2)])
def test_the_queue_matches_the_four_statement_queue(world, limit, offset, kinds):
    old = oracle.queue_for(world, limit, offset, kinds)
    assert old["total"] > 0
    assert queue.queue_for(world, limit, offset, kinds) == old


def test_the_fixture_reaches_every_kind_and_tie(world):
    """The comparison above is only as strong as what the fixture holds."""
    body = oracle.queue_for(world, 50, 0)
    assert body["by_kind"] == {
        "unmatched_message": 4,
        "unconfirmed_match": 6,
        "status_proposal": 4,
        "action_item": 2,
    }
    sent = [(i["kind"], (i["message"] or {}).get("sent_at")) for i in body["items"]]
    tied = {s for _, s in sent if s and sum(1 for _, t in sent if t == s) > 1}
    assert tied, "no tie on sent_at across kinds"


def test_queue_events_are_the_boards_events(world):
    """The queue reads stage from its own rows; the board reads
    `events_by_application`. They must agree for every live application."""
    mine = queue_items.events_by_application(queue_items.current_rows(world))
    live = {
        r["id"]
        for r in db.query(
            "SELECT id FROM applications WHERE user_id = %s AND dismissed_at IS NULL", (world,)
        )
    }
    board = {
        app: [(e.id, e.kind, e.message_id) for e in events]
        for app, events in mail_pipeline.events_by_application(world).items()
        if app in live
    }
    assert {app: [tuple(e) for e in events] for app, events in mine.items()} == board
    assert board


def test_queue_proposals_are_the_review_lists_proposals(world):
    rows = queue_items.current_rows(world)
    mine = [(r.row["application_id"], r.row["event_id"]) for r in queue_items.proposal_items(rows)]
    theirs = [(p.application_id, p.event_id) for p in mail_pipeline.proposals_for(world)]
    assert mine == theirs
    assert theirs
