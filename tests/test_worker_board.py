from __future__ import annotations

from api import db, worker
from core.store import add_ai_result


def _user_id() -> int:
    return db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]


def _make_passing_job(user_id: int, url: str, source: str = "internships") -> int:
    """A job that satisfies every _materialize_passing gate: user is subscribed
    to its source, it's active, its latest closed check passed, and it passed
    the user's one enabled filter."""
    job = db.query_one(
        "INSERT INTO jobs (url, company, title, source) VALUES (%s, 'Acme', 'SWE', %s) RETURNING id",
        (url, source),
    )
    db.execute("INSERT INTO user_sources (user_id, source) VALUES (%s, %s)", (user_id, source))
    db.execute(
        "INSERT INTO user_filters (user_id, name, prompt, prompt_hash) VALUES (%s, 'f1', 'prompt', 'hash1')",
        (user_id,),
    )
    add_ai_result(url, "passed", "still open", "closed")
    add_ai_result(url, "passed", "matches", "custom", prompt_hash="hash1")
    return job["id"]


def _board_row(user_id: int, job_id: int):
    return db.query_one(
        "SELECT * FROM user_jobs WHERE user_id = %s AND job_id = %s", (user_id, job_id)
    )


# ---------------------------------------------------------------------------
# _materialize_passing
# ---------------------------------------------------------------------------


def test_materialize_passing_recreates_a_deleted_row(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-1"
    job_id = _make_passing_job(user_id, url)

    assert worker._materialize_passing(user_id) == 1
    assert _board_row(user_id, job_id) is not None

    db.execute("DELETE FROM user_jobs WHERE user_id = %s AND job_id = %s", (user_id, job_id))
    assert _board_row(user_id, job_id) is None

    assert worker._materialize_passing(user_id) == 1
    assert _board_row(user_id, job_id) is not None


def test_materialize_passing_leaves_hidden_row_alone(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-2"
    job_id = _make_passing_job(user_id, url)

    worker._materialize_passing(user_id)
    db.execute(
        "UPDATE user_jobs SET hidden = true WHERE user_id = %s AND job_id = %s", (user_id, job_id)
    )

    assert worker._materialize_passing(user_id) == 0
    row = _board_row(user_id, job_id)
    assert row is not None
    assert row["hidden"] is True


# ---------------------------------------------------------------------------
# _demote_closed
# ---------------------------------------------------------------------------


def test_demote_closed_removes_untouched_row_when_closed_now_rejected(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-3"
    job_id = _make_passing_job(user_id, url)
    worker._materialize_passing(user_id)

    add_ai_result(url, "rejected", "now closed", "closed")

    assert worker._demote_closed() == 1
    assert _board_row(user_id, job_id) is None


def test_demote_closed_leaves_touched_row_even_when_closed(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-4"
    job_id = _make_passing_job(user_id, url)
    worker._materialize_passing(user_id)
    db.execute(
        "UPDATE user_jobs SET status = 'applied' WHERE user_id = %s AND job_id = %s",
        (user_id, job_id),
    )

    add_ai_result(url, "rejected", "now closed", "closed")

    assert worker._demote_closed() == 0
    assert _board_row(user_id, job_id) is not None


def test_demote_closed_leaves_untouched_row_when_still_open(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-5"
    job_id = _make_passing_job(user_id, url)
    worker._materialize_passing(user_id)

    assert worker._demote_closed() == 0
    assert _board_row(user_id, job_id) is not None
