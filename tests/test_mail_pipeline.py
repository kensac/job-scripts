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
