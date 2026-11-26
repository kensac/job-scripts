"""latest_posting_checks: writers only mark urls dirty; a fold brings the
projection to `verdicts`, and a fold over everything repairs what the
triggers could not see."""

from __future__ import annotations

from api import db
from core import latest_posting_checks
from core.pool import pool, transaction

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
    rows = db.query(f"SELECT * FROM ({latest_posting_checks._LATEST.format(jobs='')}) t")
    return {(r["job_id"], r["check_type"]): (r["verdict_id"], r["status"]) for r in rows}


def _dirty() -> list[str]:
    return [r["url"] for r in db.query("SELECT url FROM latest_posting_checks_dirty ORDER BY id")]


def test_a_write_only_marks_its_url_and_the_fold_takes_it_in():
    job = _job()
    first = _answer("closed", "passed")
    assert _stored() == {}
    assert set(_dirty()) == {URL}

    assert latest_posting_checks.fold() == {"written": 1, "removed": 0, "cleared": 2}
    assert _stored() == {(job, "closed"): (first, "passed")} == _truth()
    assert _dirty() == []

    second = _answer("closed", "rejected")
    latest_posting_checks.fold()
    assert _stored() == {(job, "closed"): (second, "rejected")} == _truth()


def test_the_highest_answer_wins_whatever_order_they_arrive_in():
    job = _job()
    _answer("closed", "rejected", answer_id=900_000)
    _answer("closed", "passed", answer_id=800_000)
    latest_posting_checks.fold()
    assert _stored() == {(job, "closed"): (900_000, "rejected")}


def test_deleting_the_latest_falls_back_and_deleting_all_removes_the_row():
    job = _job()
    older = _answer("clearance", "passed")
    newer = _answer("clearance", "rejected")
    latest_posting_checks.fold()
    db.execute("DELETE FROM ai_queries WHERE id = %s", (newer,))
    latest_posting_checks.fold()
    assert _stored() == {(job, "clearance"): (older, "passed")}
    db.execute("DELETE FROM ai_queries WHERE id = %s", (older,))
    assert latest_posting_checks.fold() == {"written": 0, "removed": 1, "cleared": 1}
    assert _stored() == {}


def test_an_updated_answer_is_folded_in():
    job = _job()
    answer = _answer("closed", "passed")
    latest_posting_checks.fold()
    db.execute("UPDATE ai_queries SET status = 'rejected' WHERE id = %s", (answer,))
    latest_posting_checks.fold()
    assert _stored() == {(job, "closed"): (answer, "rejected")}
    db.execute("UPDATE ai_queries SET status = 'error' WHERE id = %s", (answer,))
    latest_posting_checks.fold()
    assert _stored() == {}


def test_a_posting_added_after_its_answers_takes_them_in():
    answer = _answer("closed", "passed")
    latest_posting_checks.fold()
    assert _stored() == {}
    job = _job()
    assert _dirty() == [URL]
    latest_posting_checks.fold()
    assert _stored() == {(job, "closed"): (answer, "passed")}


def test_custom_filters_errors_and_page_rows_are_not_posting_checks():
    _job()
    latest_posting_checks.fold()
    _answer("custom", "passed", prompt_hash="h")
    assert _dirty() == []
    _answer("closed", "error")
    _answer("content", "passed")
    latest_posting_checks.fold()
    assert _stored() == {}


def test_the_fold_over_everything_repairs_what_the_triggers_did_not_see():
    job = _job()
    kept = _answer("closed", "rejected")
    changed = _answer("clearance", "passed")
    gone = _answer("closed", "passed")
    latest_posting_checks.fold()
    # Triggers off, as a replica-mode restore would write.
    with transaction():
        db.execute("SET LOCAL session_replication_role = replica")
        missed = _answer("other", "passed")
        db.execute("UPDATE ai_queries SET status = 'rejected' WHERE id = %s", (changed,))
        db.execute("DELETE FROM ai_queries WHERE id = %s", (gone,))
    assert _dirty() == []
    assert latest_posting_checks.fold() == {"written": 0, "removed": 0, "cleared": 0}

    assert latest_posting_checks.fold(everything=True) == {
        "written": 3,
        "removed": 0,
        "cleared": 0,
    }
    assert (
        _stored()
        == _truth()
        == {
            (job, "closed"): (kept, "rejected"),
            (job, "clearance"): (changed, "rejected"),
            (job, "other"): (missed, "passed"),
        }
    )
    assert latest_posting_checks.fold(everything=True) == {
        "written": 0,
        "removed": 0,
        "cleared": 0,
    }


def test_a_fold_waits_for_the_one_running_and_then_runs():
    """A skipped fold would drop the day's full fold whenever the cycle's
    fold held the lock."""
    import threading

    _job()
    _answer("closed", "passed")
    results: list[dict[str, int]] = []
    with pool.connection() as other:
        other.autocommit = True
        other.execute(f"SELECT pg_advisory_lock({latest_posting_checks._LOCK_KEY})")
        waiter = threading.Thread(target=lambda: results.append(latest_posting_checks.fold()))
        try:
            waiter.start()
            waiter.join(timeout=2)
            assert waiter.is_alive() and results == []
        finally:
            other.execute(f"SELECT pg_advisory_unlock({latest_posting_checks._LOCK_KEY})")
            other.autocommit = False
        waiter.join(timeout=30)
    assert results == [{"written": 1, "removed": 0, "cleared": 2}]
    assert _dirty() == []
