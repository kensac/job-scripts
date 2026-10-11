from __future__ import annotations

import datetime

from api import db
from tasks.mail.matching import handler as task
from tests.mail_match_support import _application, _event, _message


def test_derived_applied_at_does_not_veto_its_own_earlier_evidence(f):
    """The floor was the earliest TITLED message, then used to rule out the
    title-less ones - 159 of 161 blocked messages carry no role_title, and
    Battelle missed by four seconds. The date was derived from a subset of the
    evidence and then used against the rest of it."""
    uid = f.make_user()
    early = datetime.datetime(2026, 3, 1, 9, 0, tzinfo=datetime.UTC)
    later = datetime.datetime(2026, 3, 1, 9, 0, 4, tzinfo=datetime.UTC)

    bare = _message(uid, sent_at=early)
    _event(bare, "acknowledgement", company="Battelle")
    titled = _message(uid, sent_at=later)
    _event(titled, "acknowledgement", company="Battelle", title="Software Engineer")

    created, _ = task.seed_from_mail(uid)
    assert created == 1
    row = db.query_one("SELECT applied_at FROM applications WHERE user_id = %s", (uid,))
    assert row["applied_at"] == early, "the floor must include the evidence it was derived from"


def test_titleless_evidence_is_not_guessed_at_an_ambiguous_company(f):
    """Two roles at one employer: a message naming neither cannot be assigned
    to one of them. Lowering both floors on its say-so would replace a silent
    wrong answer with a different silent wrong answer."""
    uid = f.make_user()
    early = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    later = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)

    bare = _message(uid, sent_at=early)
    _event(bare, "acknowledgement", company="Acme")
    for role in ("Backend Engineer", "Data Engineer"):
        mid = _message(uid, sent_at=later)
        _event(mid, "acknowledgement", company="Acme", title=role)

    created, _ = task.seed_from_mail(uid)
    assert created == 2
    dates = {
        r["applied_at"]
        for r in db.query("SELECT applied_at FROM applications WHERE user_id = %s", (uid,))
    }
    assert dates == {later}, "an ambiguous message must not lower either floor"


def test_an_existing_derived_floor_is_lowered_to_all_its_evidence(f):
    """seed_from_mail applies the rule at the moment of creation, so
    correcting that rule reached none of the 1,813 applications that already
    existed. A fix that cannot reach its own population reads as done."""
    uid = f.make_user()
    early = datetime.datetime(2026, 3, 1, 9, 0, tzinfo=datetime.UTC)
    late = datetime.datetime(2026, 3, 1, 9, 0, 4, tzinfo=datetime.UTC)

    app = _application(
        uid, company="Battelle", title="Software Engineer", applied_at=late, provenance="tracker"
    )
    bare = _message(uid, sent_at=early)
    _event(bare, "acknowledgement", company="Battelle")

    # A tracker date is the user's own, so it is a valid floor and stays put.
    assert task.recompute_derived_floors(uid) == 0
    db.execute("UPDATE applications SET source_provenance = 'email' WHERE id = %s", (app,))
    assert task.recompute_derived_floors(uid) == 1

    row = db.query_one("SELECT applied_at FROM applications WHERE id = %s", (app,))
    assert row["applied_at"] == early, "the floor must reach the evidence that was excluded"


def test_recomputing_floors_twice_changes_nothing_the_second_time(f):
    """It runs every sweep rather than once, so it has to be idempotent by
    construction - a floor already at its earliest evidence does not move."""
    uid = f.make_user()
    early = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
    _application(uid, company="Acme", title="Engineer", applied_at=early)
    mid = _message(uid, sent_at=early)
    _event(mid, "acknowledgement", company="Acme")

    assert task.recompute_derived_floors(uid) == 0
    assert task.recompute_derived_floors(uid) == 0


def test_a_floor_is_never_raised(f):
    """A later message is not evidence the application started later, and
    raising a floor would invalidate matches already made against it."""
    uid = f.make_user()
    early = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    app = _application(uid, company="Acme", title="Engineer", applied_at=early)
    db.execute("UPDATE applications SET source_provenance = 'email' WHERE id = %s", (app,))
    mid = _message(uid, sent_at=datetime.datetime(2026, 8, 1, tzinfo=datetime.UTC))
    _event(mid, "acknowledgement", company="Acme")

    assert task.recompute_derived_floors(uid) == 0
    row = db.query_one("SELECT applied_at FROM applications WHERE id = %s", (app,))
    assert row["applied_at"] == early


def test_an_ambiguous_company_lowers_no_floor(f):
    """Two derived applications at one employer: a message naming no role
    could belong to either, and lowering one on its say-so is the guess
    seed_from_mail already refuses to make."""
    uid = f.make_user()
    late = datetime.datetime(2026, 6, 1, tzinfo=datetime.UTC)
    for role in ("Backend Engineer", "Data Engineer"):
        _application(uid, company="Acme", title=role, applied_at=late)
    mid = _message(uid, sent_at=datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC))
    _event(mid, "acknowledgement", company="Acme")

    assert task.recompute_derived_floors(uid) == 0
    dates = {
        r["applied_at"]
        for r in db.query("SELECT applied_at FROM applications WHERE user_id = %s", (uid,))
    }
    assert dates == {late}
