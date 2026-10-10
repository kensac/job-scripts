"""A managed board rebuild writes only the rows that changed, and ends with
exactly the rows the delete-and-reinsert writer produced.

The rebuild used to DELETE every row of the board and INSERT them again:
78,371 inserts and 78,019 deletes on a 1,332-row table (pg_stat_user_tables,
production, 2026-10-10). `_old_write` is that writer, verbatim, kept as the
oracle: every scenario runs both from the same starting rows and compares.
"""

from __future__ import annotations

import datetime
import threading

import pytest

from api import db, managed_board_runs
from api.db import pool
from tests.test_public_job_lists import _published_board

MODEL = "gpt-5.6-luna"
HASH = "hash"
T0 = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)


def _old_write(payload: dict, ids: list[int], sort_at: list) -> None:
    """The writer before the diff: delete the board, insert the members."""
    with db.transaction():
        board = payload["managed_board_id"]
        db.execute("DELETE FROM managed_board_jobs WHERE managed_board_id = %s", (board,))
        if payload.get("execution_mode") == "sponsor_filter_reuse":
            db.execute(
                """
                WITH candidate AS (
                  SELECT * FROM unnest(%(ids)s::bigint[], %(sort)s::timestamptz[]) AS c(job_id, sort_at)
                )
                INSERT INTO managed_board_jobs (managed_board_id, job_id, sort_at)
                SELECT %(board)s, job_id, sort_at
                FROM candidate ORDER BY sort_at DESC, job_id DESC
                """,
                {
                    "ids": ids,
                    "sort": sort_at,
                    "board": board,
                },
            )
            return
        db.execute(
            """
            WITH candidate AS (
              SELECT * FROM unnest(%(ids)s::bigint[], %(sort)s::timestamptz[]) AS c(job_id, sort_at)
            ), latest AS (
              SELECT DISTINCT ON (j.id) j.id AS job_id, q.status
              FROM candidate c JOIN jobs j ON j.id = c.job_id
              LEFT JOIN ai_queries q ON q.url = j.url AND q.check_type = 'custom'
                AND q.prompt_hash = %(hash)s AND q.model = %(model)s
                AND q.status IN ('passed', 'rejected', 'failed')
              ORDER BY j.id, q.id DESC NULLS LAST
            )
            INSERT INTO managed_board_jobs (managed_board_id, job_id, sort_at)
            SELECT %(board)s, c.job_id, c.sort_at
            FROM candidate c LEFT JOIN latest l USING (job_id)
            WHERE l.status = 'passed' OR (NOT %(fail_closed)s AND l.status IS DISTINCT FROM 'rejected')
            ORDER BY c.sort_at DESC, c.job_id DESC
            """,
            {
                "ids": ids,
                "sort": sort_at,
                "hash": payload["prompt_hash"],
                "model": payload["requested_model"],
                "board": board,
                "fail_closed": payload["fail_closed"],
            },
        )


def _rows() -> list[tuple]:
    return [
        tuple(r.values())
        for r in db.query(
            "SELECT managed_board_id, job_id, sort_at "
            "FROM managed_board_jobs ORDER BY managed_board_id, job_id"
        )
    ]


def _physical() -> dict[tuple[int, int], str]:
    """Where each row lives and which transaction wrote it."""
    rows = db.query(
        "SELECT managed_board_id, job_id, ctid::text || '/' || xmin::text AS at "
        "FROM managed_board_jobs"
    )
    return {(r["managed_board_id"], r["job_id"]): r["at"] for r in rows}


def _seed(rows: list[tuple]) -> None:
    db.execute("DELETE FROM managed_board_jobs")
    for row in rows:
        db.execute(
            "INSERT INTO managed_board_jobs "
            "(managed_board_id, job_id, sort_at) VALUES (%s, %s, %s)",
            row,
        )


def _fixture(f):
    """A published board, a second board, and seven jobs whose latest
    verdicts cover every branch of the membership rule."""
    board = _published_board()
    other = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model) SELECT 'other', 'Other', sponsor_user_id, 'p', 'h2', %s "
        "FROM managed_boards WHERE id = %s RETURNING id",
        (MODEL, board),
    )["id"]
    jobs = [f.make_job() for _ in range(7)]
    urls = {r["id"]: r["url"] for r in db.query("SELECT id, url FROM jobs")}
    # 0 passed, 1 rejected, 2 failed, 3 none, 4 rejected then passed,
    # 5 passed then rejected, 6 passed under another model (so: none).
    verdicts = [
        (0, "passed", MODEL),
        (1, "rejected", MODEL),
        (2, "failed", MODEL),
        (4, "rejected", MODEL),
        (4, "passed", MODEL),
        (5, "passed", MODEL),
        (5, "rejected", MODEL),
        (6, "passed", "other-model"),
    ]
    for index, status, model in verdicts:
        db.execute(
            "INSERT INTO ai_queries (url, check_type, prompt_hash, model, status) "
            "VALUES (%s, 'custom', %s, %s, %s)",
            (urls[jobs[index]], HASH, model, status),
        )
    return board, other, jobs


def _payload(board: int, jobs: list[int], mode: str, fail_closed: bool) -> dict:
    return {
        "managed_board_id": board,
        "revision": 1,
        "execution_mode": mode,
        "prompt_hash": HASH,
        "requested_model": MODEL,
        "fail_closed": fail_closed,
        # Job 6 sorts later than the stale row seeded for it.
        "jobs": [
            {"id": job, "sort_at": (T0 + datetime.timedelta(days=i)).isoformat()}
            for i, job in enumerate(jobs[:6])
        ]
        + [{"id": jobs[6], "sort_at": (T0 + datetime.timedelta(days=30)).isoformat()}],
    }


def _starts(board: int, other: int, jobs: list[int]) -> dict[str, list[tuple]]:
    day = datetime.timedelta(days=1)
    return {
        "empty": [],
        "stale": [
            # Unchanged under the reuse and fail-open runs.
            (board, jobs[0], T0),
            # A member the filter runs drop.
            (board, jobs[1], T0 + day),
            # Moved: a stale sort.
            (board, jobs[3], T0),
            (board, jobs[6], T0),
            (board, jobs[4], T0 + 4 * day),
            # Another board's row, which no run of this board touches.
            (other, jobs[2], T0),
        ],
    }


@pytest.mark.parametrize(
    "mode,fail_closed",
    [
        ("sponsor_filter_reuse", False),
        ("managed_filter", False),
        ("managed_filter", True),
    ],
)
@pytest.mark.parametrize("start", ["empty", "stale"])
def test_the_diff_write_ends_with_the_rows_the_full_rewrite_wrote(f, mode, fail_closed, start):
    board, other, jobs = _fixture(f)
    payload = _payload(board, jobs, mode, fail_closed)
    seed = _starts(board, other, jobs)[start]
    ids = [job["id"] for job in payload["jobs"]]
    sort_at = [job["sort_at"] for job in payload["jobs"]]

    _seed(seed)
    _old_write(payload, ids, sort_at)
    expected = _rows()

    _seed(seed)
    before = _physical()
    n = managed_board_runs.replace_projection(payload)

    assert _rows() == expected
    assert n == len([row for row in expected if row[0] == board])
    # Rows already right, this board's and the other board's, are not written.
    after = _physical()
    unchanged = {row[:2] for row in seed if row in expected}
    assert {key: after[key] for key in unchanged} == {key: before[key] for key in unchanged}
    if start == "stale":
        assert (board, jobs[0]) in unchanged and (other, jobs[2]) in unchanged


def test_an_unchanged_board_writes_no_rows(f):
    board, _other, jobs = _fixture(f)
    payload = _payload(board, jobs, "managed_filter", False)
    managed_board_runs.replace_projection(payload)
    before = _physical()
    projected = db.query("SELECT job_id, projected_at FROM managed_board_jobs ORDER BY job_id")

    managed_board_runs.replace_projection(payload)

    assert before and _physical() == before
    # projected_at is when the posting joined, so it holds still too.
    assert db.query("SELECT job_id, projected_at FROM managed_board_jobs ORDER BY job_id") == (
        projected
    )


def test_a_public_reader_sees_the_old_board_until_the_rebuild_commits(f, client, monkeypatch):
    board, _other, jobs = _fixture(f)
    _seed([(board, jobs[1], T0), (board, jobs[2], T0)])
    payload = _payload(board, jobs, "managed_filter", True)
    written = threading.Event()
    release = threading.Event()
    execute = db.execute

    def pausing(sql, params=None):
        # The board's own row is updated after its members are written and
        # before the transaction commits.
        if sql.startswith("UPDATE managed_boards SET projection_updated_at"):
            written.set()
            assert release.wait(30)
        return execute(sql, params)

    monkeypatch.setattr(db, "execute", pausing)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            managed_board_runs.replace_projection(payload)
        except BaseException as e:
            errors.append(e)

    def listed() -> set[int]:
        response = client.get("/v1/public/job-lists/engineering", params={"limit": 50})
        assert response.status_code == 200
        return {job["job_id"] for job in response.json()["jobs"]}

    writer = threading.Thread(target=run)
    writer.start()
    try:
        assert written.wait(30)
        with pool.connection() as reader:
            assert {
                r["job_id"]
                for r in reader.execute(
                    "SELECT job_id FROM managed_board_jobs WHERE managed_board_id = %s", (board,)
                )
            } == {jobs[1], jobs[2]}
        assert listed() == {jobs[1], jobs[2]}
    finally:
        release.set()
        writer.join(30)
    assert not errors
    assert listed() == {jobs[0], jobs[4]}
