"""latest_posting_checks: exact through the triggers on every write, and
brought back by the reconcile when a write slips past them."""

from __future__ import annotations

from api import db
from core import latest_posting_checks
from core.pool import transaction

URL = "https://jobs.test/posting"


def _job(url: str = URL) -> int:
    row = db.query_one(
        "INSERT INTO jobs (url, raw_url, source, company, title, active, available) "
        "VALUES (%s, %s, 'src', 'Acme', 'Engineer', true, true) RETURNING id",
        (url, url),
    )
    assert row is not None
    return row["id"]


def _answer(check: str, status: str, url: str = URL, answer_id: int | None = None, **extra) -> int:
    cols = {"url": url, "check_type": check, "status": status, **extra}
    if answer_id is not None:
        cols["id"] = answer_id
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    row = db.query_one(
        f"INSERT INTO ai_queries ({names}) VALUES ({marks}) RETURNING id", tuple(cols.values())
    )
    assert row is not None
    return row["id"]


def _stored() -> dict[tuple[int, str], tuple[int, str]]:
    return {
        (r["job_id"], r["check_type"]): (r["verdict_id"], r["status"])
        for r in db.query("SELECT * FROM latest_posting_checks")
    }


def _truth() -> dict[tuple[int, str], tuple[int, str]]:
    rows = db.query(f"SELECT * FROM ({latest_posting_checks._LATEST}) t")
    return {(r["job_id"], r["check_type"]): (r["verdict_id"], r["status"]) for r in rows}


def test_an_answer_becomes_the_latest_and_a_newer_one_replaces_it():
    job = _job()
    first = _answer("closed", "passed")
    assert _stored() == {(job, "closed"): (first, "passed")}
    second = _answer("closed", "rejected")
    assert _stored() == {(job, "closed"): (second, "rejected")}
    assert _stored() == _truth()


def test_an_older_answer_committed_later_does_not_replace_a_newer_one():
    job = _job()
    _answer("closed", "rejected", answer_id=900_000)
    _answer("closed", "passed", answer_id=800_000)
    assert _stored() == {(job, "closed"): (900_000, "rejected")}


def test_deleting_the_latest_falls_back_and_deleting_all_removes_the_row():
    job = _job()
    older = _answer("clearance", "passed")
    newer = _answer("clearance", "rejected")
    db.execute("DELETE FROM ai_queries WHERE id = %s", (newer,))
    assert _stored() == {(job, "clearance"): (older, "passed")}
    db.execute("DELETE FROM ai_queries WHERE id = %s", (older,))
    assert _stored() == {}


def test_an_update_is_recomputed():
    job = _job()
    answer = _answer("closed", "passed")
    db.execute("UPDATE ai_queries SET status = 'rejected' WHERE id = %s", (answer,))
    assert _stored() == {(job, "closed"): (answer, "rejected")}
    db.execute("UPDATE ai_queries SET status = 'error' WHERE id = %s", (answer,))
    assert _stored() == {}


def test_a_posting_added_after_its_answers_starts_with_them():
    answer = _answer("closed", "passed")
    job = _job()
    assert _stored() == {(job, "closed"): (answer, "passed")}


def test_custom_filters_errors_and_page_rows_are_not_posting_checks():
    _job()
    _answer("custom", "passed", prompt_hash="h")
    _answer("closed", "error")
    assert _stored() == {}


def test_the_reconcile_fills_what_the_triggers_did_not_see_and_then_does_nothing():
    job = _job()
    # Triggers off for this one write, as a replica-mode restore would be.
    with transaction():
        db.execute("SET LOCAL session_replication_role = replica")
        missed = _answer("closed", "passed")
    assert _stored() == {}

    assert latest_posting_checks.reconcile() == {"filled": 1, "removed": 0, "replaced": 0}
    assert _stored() == {(job, "closed"): (missed, "passed")}
    assert latest_posting_checks.reconcile() == {"filled": 0, "removed": 0, "replaced": 0}


def test_the_reconcile_repairs_a_row_whose_answer_was_deleted_unseen():
    job = _job()
    older = _answer("closed", "rejected")
    newer = _answer("closed", "passed")
    with transaction():
        db.execute("SET LOCAL session_replication_role = replica")
        db.execute("DELETE FROM ai_queries WHERE id = %s", (newer,))
    assert _stored() == {(job, "closed"): (newer, "passed")}

    assert latest_posting_checks.reconcile() == {"filled": 0, "removed": 0, "replaced": 1}
    assert _stored() == {(job, "closed"): (older, "rejected")} == _truth()
