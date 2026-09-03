"""Application state, derived from events rather than stored.

The three awkward cases - applied before the posting reached the board, an
email misfiled and corrected later, a rejection arriving before its own
acknowledgement - all stop being special when state is recomputed. These
tests are mostly those cases.
"""

from __future__ import annotations

import datetime

from api import db, mail_pipeline


def _app(user_id: int, **kw) -> int:
    row = db.query_one(
        "INSERT INTO applications (user_id, company_name, title) VALUES (%s, %s, %s) RETURNING id",
        (user_id, kw.get("company", "Acme"), kw.get("title", "Engineer")),
    )
    assert row is not None
    return row["id"]


def _message(user_id: int, mid: str, sent_at=None) -> int:
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, sent_at) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (user_id, mid, "gmail", sent_at or datetime.datetime.now(datetime.UTC)),
    )
    assert row is not None
    return row["id"]


def _event(message_id: int, kind: str, deadline=None) -> int:
    row = db.query_one(
        "INSERT INTO email_events (message_id, kind, deadline_at) VALUES (%s, %s, %s) RETURNING id",
        (message_id, kind, deadline),
    )
    assert row is not None
    return row["id"]


def _match(message_id: int, application_id: int | None) -> None:
    db.execute(
        "INSERT INTO application_matches (message_id, application_id, method) VALUES (%s, %s, %s)",
        (message_id, application_id, "ats_company"),
    )


def _chain(f, kinds: list[str]) -> tuple[int, int]:
    uid = f.make_user()
    app = _app(uid)
    for i, kind in enumerate(kinds):
        mid = _message(uid, f"<p{i}-{app}@x>")
        _event(mid, kind)
        _match(mid, app)
    return uid, app


def test_stage_advances_with_the_process(f):
    _uid, app = _chain(f, ["acknowledgement", "interview_invite"])
    assert mail_pipeline.state_of(app)["stage"] == "interviewing"


def test_a_rejection_wins_over_a_later_acknowledgement(f):
    """ATS systems send automated acknowledgements on a schedule unrelated to
    the decision, so one arriving after a rejection is untidy mail delivery,
    not the employer changing their mind."""
    _uid, app = _chain(f, ["rejection", "acknowledgement"])
    assert mail_pipeline.state_of(app)["stage"] == "rejected"


def test_stage_does_not_regress_on_an_out_of_order_event(f):
    _uid, app = _chain(f, ["interview_invite", "acknowledgement"])
    assert mail_pipeline.state_of(app)["stage"] == "interviewing"


def test_reclassifying_a_message_changes_the_state(f):
    """Classification is append-only, so a correction appends a newer verdict.
    Only the newest per (message, kind) counts - which is what makes fixing a
    misclassification a recomputation rather than a repair."""
    uid = f.make_user()
    app = _app(uid)
    mid = _message(uid, "<reclass@x>")
    _event(mid, "rejection")
    _match(mid, app)
    assert mail_pipeline.state_of(app)["stage"] == "rejected"

    # Same message, same kind, newer row: the earlier verdict is superseded.
    db.execute("INSERT INTO email_events (message_id, kind) VALUES (%s, %s)", (mid, "rejection"))
    assert mail_pipeline.state_of(app)["stage"] == "rejected"

    # A correction: the same message reclassified. The rejection must be
    # RETRACTED, not left standing beside the new verdict - a message is one
    # thing, and keying per (message, kind) would make a misclassification
    # permanent. This assertion is what caught that.
    _event(mid, "interview_invite")
    assert mail_pipeline.state_of(app)["stage"] == "interviewing"


def test_only_the_newest_match_counts(f):
    """Matches are append-only too. A message rematched to a different
    application must stop contributing to the old one."""
    uid = f.make_user()
    first, second = _app(uid), _app(uid, company="Other")
    mid = _message(uid, "<rematch@x>")
    _event(mid, "offer")
    _match(mid, first)
    assert mail_pipeline.state_of(first)["stage"] == "offer"

    _match(mid, second)
    assert mail_pipeline.state_of(first)["stage"] == "applied"
    assert mail_pipeline.state_of(second)["stage"] == "offer"


def test_an_assessment_opens_an_action_item(f):
    uid = f.make_user()
    app = _app(uid)
    mid = _message(uid, "<oa@x>")
    _event(mid, "assessment_invite", deadline=datetime.datetime(2026, 9, 9, tzinfo=datetime.UTC))
    _match(mid, app)
    assert mail_pipeline.sync_action_items(app)["opened"] == 1
    item = db.query_one("SELECT * FROM action_items WHERE application_id = %s", (app,))
    assert item is not None
    assert item["kind"] == "complete_assessment"
    assert item["due_at"] is not None


def test_a_later_event_resolves_it_without_the_user_touching_anything(f):
    """This is what makes the system no-touch. An assessment invite is closed
    by the acknowledgement that follows it, not by remembering to tick it."""
    uid = f.make_user()
    app = _app(uid)
    oa = _message(uid, "<oa2@x>")
    _event(oa, "assessment_invite")
    _match(oa, app)
    mail_pipeline.sync_action_items(app)

    done = _message(uid, "<done@x>")
    _event(done, "acknowledgement")
    _match(done, app)
    assert mail_pipeline.sync_action_items(app)["resolved"] == 1

    item = db.query_one("SELECT * FROM action_items WHERE application_id = %s", (app,))
    assert item is not None
    assert item["resolved_at"] is not None
    assert item["resolved_by_event_id"] is not None


def test_syncing_twice_opens_nothing_new(f):
    """It runs on every recomputation, so duplicating would fill the list with
    the same task repeatedly."""
    uid = f.make_user()
    app = _app(uid)
    mid = _message(uid, "<idem@x>")
    _event(mid, "interview_invite")
    _match(mid, app)
    assert mail_pipeline.sync_action_items(app)["opened"] == 1
    assert mail_pipeline.sync_action_items(app)["opened"] == 0


def test_an_earlier_event_does_not_resolve_a_later_ask(f):
    """Resolution must come from an event AFTER the ask. An acknowledgement
    that predates the assessment invite settles nothing."""
    uid = f.make_user()
    app = _app(uid)
    ack = _message(uid, "<early-ack@x>")
    _event(ack, "acknowledgement")
    _match(ack, app)
    oa = _message(uid, "<late-oa@x>")
    _event(oa, "assessment_invite")
    _match(oa, app)

    mail_pipeline.sync_action_items(app)
    assert mail_pipeline.sync_action_items(app)["resolved"] == 0


def test_detaching_a_message_closes_the_action_it_asked_for(f):
    """An item whose event no longer reaches the application is stranded:
    nothing will ever resolve it, and it stays open forever asking for
    something about an application it is not part of."""
    from api import mail_match
    from api.mail_pipeline import sync_action_items

    uid = f.make_user()
    app = db.query_one(
        "INSERT INTO applications (user_id, company_name, title, source_provenance) "
        "VALUES (%s,'Acme','Engineer','tracker') RETURNING id",
        (uid,),
    )["id"]
    msg = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, subject) "
        "VALUES (%s,'strand-1','takeout','Assessment') RETURNING id",
        (uid,),
    )["id"]
    db.execute(
        "INSERT INTO email_events (message_id, kind, confidence) VALUES (%s,'assessment_invite','high')",
        (msg,),
    )
    mail_match.record(msg, mail_match.Match(app, "ats_company", "medium", "test"))

    assert sync_action_items(app)["opened"] == 1

    mail_match.record(msg, mail_match.Match(None, "detached", "none", "wrong application"))
    result = sync_action_items(app)
    assert result["resolved"] == 1
    row = db.query_one(
        "SELECT resolved_at, resolution FROM action_items WHERE application_id = %s", (app,)
    )
    assert row["resolved_at"] is not None
    assert "no longer part of this application" in row["resolution"]


def test_withdrawn_is_the_one_stage_only_the_person_can_assert(f):
    """It was declared vocabulary with no producer: TERMINAL named it, the API
    served it, the frontend rendered a column for it, and nothing could ever
    reach it - because no employer sends mail saying you pulled out."""
    from api.mail_pipeline import WITHDRAWN_STATUSES, stage_for

    events = [{"id": 1, "kind": "acknowledgement", "sent_at": None}]
    assert stage_for(events) == "acknowledged"
    for status in WITHDRAWN_STATUSES:
        assert stage_for(events, status) == "withdrawn"


def test_withdrawing_beats_a_later_acknowledgement(f):
    """ATS systems send acknowledgements on a schedule that has nothing to do
    with the decision. Withdrawal is asserted by the person rather than
    inferred from what an employer sent, so it outranks all of it."""
    from api.mail_pipeline import stage_for

    events = [
        {"id": 1, "kind": "rejection", "sent_at": None},
        {"id": 2, "kind": "acknowledgement", "sent_at": None},
    ]
    assert stage_for(events) == "rejected"
    assert stage_for(events, "No Longer Interested") == "withdrawn"


def test_every_declared_stage_has_something_that_produces_it():
    """A stage the API names and nothing can reach is a column the frontend
    renders forever at zero, and a total its parts never sum to."""
    from api import mail_pipeline

    declared = set(mail_pipeline.STAGE_ORDER) | set(mail_pipeline.TERMINAL)
    from_events = set(mail_pipeline._EVENT_TO_STAGE.values())
    # "applied" is the floor stage_for falls back to; "withdrawn" comes from the
    # board rather than from mail. Everything else must have an event.
    producible = from_events | {"applied", "withdrawn"}
    assert declared <= producible, f"no producer for {sorted(declared - producible)}"


def test_settles_on_separates_awaiting_from_never_closeable():
    """`resolved_at IS NULL` means two different things and the difference is
    the whole product.

    An assessment invite is awaiting an event that may still arrive. An offer
    is not: measured over the corpus, of 71 applications carrying an offer
    event only 11 have ANY later event, and no kind follows one reliably —
    because no email says "you accepted". Rendering both as open asserts a
    live obligation for the second that has never existed.
    """
    from api.mail_pipeline import settles_on

    assert "acknowledgement" in settles_on("complete_assessment")
    assert "interview_scheduled" in settles_on("schedule_interview")
    # Deliberately never-closeable: nothing observable settles either.
    assert settles_on("respond_to_offer") == ["rejection"]
    assert settles_on("reply_to_recruiter") == []
    assert settles_on("no_such_kind") == []


def test_every_action_kind_declares_what_settles_it():
    """A kind that opens items but is absent from _RESOLVING_EVENTS would be
    silently never-closeable, which is the same defect as an empty list but
    without saying so."""
    from api.mail_pipeline import _EVENT_TO_ACTION, _RESOLVING_EVENTS

    assert set(_EVENT_TO_ACTION.values()) <= set(_RESOLVING_EVENTS), (
        "an action kind with no _RESOLVING_EVENTS entry cannot declare itself never-closeable"
    )
