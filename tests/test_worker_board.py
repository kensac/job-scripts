from __future__ import annotations

from api import db
from core import catalog
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


def _working_set_row(user_id: int, job_id: int):
    return db.query_one(
        "SELECT * FROM user_job_working_set WHERE user_id = %s AND job_id = %s",
        (user_id, job_id),
    )


# ---------------------------------------------------------------------------
# materialize_passing
# ---------------------------------------------------------------------------


def _picked(user_id: int, job_id: int) -> None:
    db.execute(
        "INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)", (user_id, job_id)
    )


def test_materialize_passing_writes_the_working_set_and_no_person_row(user_headers):
    user_id = _user_id()
    job_id = _make_passing_job(user_id, "https://jobs.example.com/board-1")

    assert tasks_board.materialize_passing(user_id) == 1
    assert _working_set_row(user_id, job_id) is not None
    assert _board_row(user_id, job_id) is None, "user_jobs holds only what a person did"


def test_materialize_passing_rejoins_a_removed_pair_and_reruns_idempotently(user_headers):
    user_id = _user_id()
    job_id = _make_passing_job(user_id, "https://jobs.example.com/rejoin")

    assert tasks_board.materialize_passing(user_id) == 1
    assert tasks_board.materialize_passing(user_id) == 0
    db.execute(
        "DELETE FROM user_job_working_set WHERE user_id = %s AND job_id = %s", (user_id, job_id)
    )
    assert tasks_board.materialize_passing(user_id) == 1
    assert _working_set_row(user_id, job_id) is not None


def test_materialize_passing_adds_scope_without_changing_existing_person_state(user_headers):
    user_id = _user_id()
    job_id = _make_passing_job(user_id, "https://jobs.example.com/person-state-first")
    db.execute(
        """
        INSERT INTO user_jobs
            (user_id, job_id, status, notes, hidden, person_touched_at, created_at, updated_at)
        VALUES (%s, %s, 'Interview', 'Keep this', true,
                '2026-09-01T12:00:00Z', '2026-08-01T12:00:00Z', '2026-09-02T12:00:00Z')
        """,
        (user_id, job_id),
    )
    before = _board_row(user_id, job_id)

    assert tasks_board.materialize_passing(user_id) == 1

    assert _working_set_row(user_id, job_id) is not None
    assert _board_row(user_id, job_id) == before


# ---------------------------------------------------------------------------
# demote_closed
# ---------------------------------------------------------------------------


def test_demote_closed_removes_untouched_row_when_closed_now_rejected(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-3"
    job_id = _make_passing_job(user_id, url)
    _picked(user_id, job_id)

    add_ai_result(url, "rejected", "now closed", "closed")

    assert tasks_board.demote_closed() == 1
    assert _board_row(user_id, job_id) is None
    assert _working_set_row(user_id, job_id) is None


def test_demote_closed_removes_scope_but_leaves_explicit_noop_person_state(client, user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-4"
    job_id = _make_passing_job(user_id, url)
    tasks_board.materialize_passing(user_id)
    response = client.patch(f"/v1/user/jobs/{job_id}", json={"hidden": False}, headers=user_headers)
    assert response.status_code == 200, response.text

    add_ai_result(url, "rejected", "now closed", "closed")

    assert tasks_board.demote_closed() == 1, "the working-set pair goes"
    person_row = _board_row(user_id, job_id)
    assert person_row is not None and person_row["person_touched_at"] is not None
    assert _working_set_row(user_id, job_id) is None


def test_demote_closed_removes_and_counts_a_working_set_only_pair(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-working-only"
    job_id = _make_passing_job(user_id, url)
    db.execute(
        "INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)",
        (user_id, job_id),
    )
    add_ai_result(url, "rejected", "now closed", "closed")

    assert tasks_board.demote_closed() == 1
    assert _board_row(user_id, job_id) is None
    assert _working_set_row(user_id, job_id) is None


def test_demote_closed_removes_both_relations_when_source_marks_inactive(user_headers):
    user_id = _user_id()
    job_id = _make_passing_job(user_id, "https://jobs.example.com/board-inactive")
    _picked(user_id, job_id)
    db.execute("UPDATE jobs SET active = false WHERE id = %s", (job_id,))

    assert tasks_board.demote_closed() == 1
    assert _board_row(user_id, job_id) is None
    assert _working_set_row(user_id, job_id) is None


def test_demote_closed_leaves_untouched_row_when_still_open(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-5"
    job_id = _make_passing_job(user_id, url)
    _picked(user_id, job_id)

    assert tasks_board.demote_closed() == 0
    assert _working_set_row(user_id, job_id) is not None


def test_demote_closed_retry_is_idempotent(user_headers):
    user_id = _user_id()
    url = "https://jobs.example.com/board-demote-retry"
    job_id = _make_passing_job(user_id, url)
    _picked(user_id, job_id)
    add_ai_result(url, "rejected", "now closed", "closed")

    assert tasks_board.demote_closed() == 1
    assert tasks_board.demote_closed() == 0
    assert _board_row(user_id, job_id) is None
    assert _working_set_row(user_id, job_id) is None


# ---------------------------------------------------------------------------
# a board row is scope, not visibility
# ---------------------------------------------------------------------------


def test_the_working_set_is_scope_and_not_what_a_person_sees(user_headers):
    """The two meanings that once shared user_jobs, pinned apart.

    A working-set pair carries scope: it makes the posting worth paying to
    check (core/store.py ON_A_BOARD) and it is where the re-verification
    sweep finds candidates. It does NOT make the posting visible, because
    visibility.FULL admits a picked posting through its structural branch.

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
    assert _working_set_row(user_id, job_id) is not None
    assert job_id in visibility.member_ids(user_id), "the pair changed nothing about seeing it"

    # And taking the pair away does not take the posting away either.
    db.execute(
        "DELETE FROM user_job_working_set WHERE user_id = %s AND job_id = %s", (user_id, job_id)
    )
    assert job_id in visibility.member_ids(user_id), (
        "a working-set pair is not what makes a posting visible; if this fails, "
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


def test_demote_closed_reads_availability_not_the_feed_flag(user_headers, f):
    """A pair goes when its posting is no longer available
    (catalog.IS_AVAILABLE), not when the last feed to upsert it said inactive."""
    user_id = _user_id()
    f.make_source("still-lists-it")
    listed = _make_passing_job(user_id, "https://jobs.example.com/board-still-listed")
    _picked(user_id, listed)
    db.execute(
        "INSERT INTO source_observations (job_id, source, kind) VALUES (%s, 'still-lists-it', 'appeared')",
        (listed,),
    )
    # A JSON feed's inactive record wrote the flag last; another source lists it.
    db.execute("UPDATE jobs SET active = false WHERE id = %s", (listed,))
    # sheet_import is a switched-off source: its flag says active, it is not.
    imported = f.make_job(url="https://jobs.example.com/board-imported", source="sheet_import")
    _picked(user_id, imported)
    # What the hourly reconcile stores (catalog.reconcile_available).
    catalog.reconcile_available()

    assert tasks_board.demote_closed() == 1
    assert _working_set_row(user_id, listed) is not None
    assert _working_set_row(user_id, imported) is None
