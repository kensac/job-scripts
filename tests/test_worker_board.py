from __future__ import annotations

from api import db
from core.store import add_ai_result
from tasks import board as tasks_board


def _user_id() -> int:
    return db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]


def _make_passing_job(user_id: int, url: str, source: str = "internships") -> int:
    """A job that satisfies every materialize_passing gate: user is subscribed
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
# materialize_passing
# ---------------------------------------------------------------------------


def test_materialize_passing_recreates_a_deleted_row(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-1"
    job_id = _make_passing_job(user_id, url)

    assert tasks_board.materialize_passing(user_id) == 1
    assert _board_row(user_id, job_id) is not None

    db.execute("DELETE FROM user_jobs WHERE user_id = %s AND job_id = %s", (user_id, job_id))
    assert _board_row(user_id, job_id) is None

    assert tasks_board.materialize_passing(user_id) == 1
    assert _board_row(user_id, job_id) is not None


def test_materialize_passing_leaves_hidden_row_alone(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-2"
    job_id = _make_passing_job(user_id, url)

    tasks_board.materialize_passing(user_id)
    db.execute(
        "UPDATE user_jobs SET hidden = true WHERE user_id = %s AND job_id = %s", (user_id, job_id)
    )

    assert tasks_board.materialize_passing(user_id) == 0
    row = _board_row(user_id, job_id)
    assert row is not None
    assert row["hidden"] is True


# ---------------------------------------------------------------------------
# demote_closed
# ---------------------------------------------------------------------------


def test_demote_closed_removes_untouched_row_when_closed_now_rejected(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-3"
    job_id = _make_passing_job(user_id, url)
    tasks_board.materialize_passing(user_id)

    add_ai_result(url, "rejected", "now closed", "closed")

    assert tasks_board.demote_closed() == 1
    assert _board_row(user_id, job_id) is None


def test_demote_closed_leaves_touched_row_even_when_closed(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-4"
    job_id = _make_passing_job(user_id, url)
    tasks_board.materialize_passing(user_id)
    db.execute(
        "UPDATE user_jobs SET status = 'applied' WHERE user_id = %s AND job_id = %s",
        (user_id, job_id),
    )

    add_ai_result(url, "rejected", "now closed", "closed")

    assert tasks_board.demote_closed() == 0
    assert _board_row(user_id, job_id) is not None


def test_demote_closed_leaves_untouched_row_when_still_open(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-5"
    job_id = _make_passing_job(user_id, url)
    tasks_board.materialize_passing(user_id)

    assert tasks_board.demote_closed() == 0
    assert _board_row(user_id, job_id) is not None


# ---------------------------------------------------------------------------
# a board row is scope, not visibility
# ---------------------------------------------------------------------------


def test_an_untouched_board_row_is_the_working_set_and_not_what_a_person_sees(user_headers):
    """The two meanings that share user_jobs, pinned apart.

    An untouched row carries scope: it makes the posting worth paying to check
    (core/store.py ON_A_BOARD) and it is where the re-verification sweep finds
    candidates. It does NOT make the posting visible, because visibility.FULL
    admits an untouched row only through its structural branch, which never
    references user_jobs.

    Reading the one as the other is how a board question got answered wrongly
    on 2026-09-10: deleting the row was said to remove the posting from the
    board, and it does not.
    """
    from api.board import visibility

    user_id = _user_id()
    job_id = _make_passing_job(user_id, "https://scope.test/1")

    # Visible with no board row at all: the structural branch decides.
    assert job_id in visibility.member_ids(user_id)
    assert _board_row(user_id, job_id) is None

    tasks_board.materialize_passing(user_id)
    assert _board_row(user_id, job_id) is not None
    assert job_id in visibility.member_ids(user_id), "the row changed nothing about seeing it"

    # And taking the row away does not take the posting away either.
    db.execute("DELETE FROM user_jobs WHERE user_id = %s AND job_id = %s", (user_id, job_id))
    assert job_id in visibility.member_ids(user_id), (
        "an untouched board row is not what makes a posting visible; if this fails, "
        "the two meanings have been merged and the comments in board.py and store.py lie"
    )


def test_a_board_row_is_what_keeps_a_posting_worth_checking(user_headers):
    """The other half: the row is scope. A posting whose source nobody
    subscribes to stays AI-eligible while somebody keeps it."""
    from core.store import AI_ELIGIBLE_JOB

    user_id = _user_id()
    job_id = _make_passing_job(user_id, "https://scope.test/2", source="unsubscribed-src")
    db.execute(
        "DELETE FROM user_sources WHERE user_id = %s AND source = 'unsubscribed-src'", (user_id,)
    )
    db.execute("INSERT INTO sources (name, listings_url) VALUES ('unsubscribed-src', 'u')")

    def eligible() -> bool:
        return bool(
            db.query_one(
                f"SELECT 1 FROM jobs j WHERE j.id = %s AND {AI_ELIGIBLE_JOB.format(job='j')}",
                (job_id,),
            )
        )

    assert not eligible(), "nobody subscribes to its source, so nothing should pay to check it"
    db.execute("INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s)", (user_id, job_id))
    assert eligible(), "somebody keeps it, so it is checked: that is what the row is for"
