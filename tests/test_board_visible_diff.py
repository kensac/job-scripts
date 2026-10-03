"""A recompute writes only the rows that changed, and the board's computed_at
still advances on every recompute.

The recompute used to DELETE every row of a person's board and INSERT them
again: 726 recomputes rewrote 1.28M rows and 3.97 GB of WAL in 36 hours of
production (pg_stat_statements, 2026-10-03). These pin the diff write against
that writer's observable behaviour, including the old writer itself, which
keeps running on workers that have not rolled yet.
"""

from __future__ import annotations

import threading

from api import db
from api.board import populations, visibility
from api.db import pool
from tests.test_api_jobs import _insert_job, _pass_closed, _subscribe


def _old_recompute(conn, user_id: int, ids: list[int]) -> None:
    """The writer before the diff, verbatim: what an unrolled worker runs."""
    conn.execute("SELECT pg_advisory_xact_lock(%s, %s)", (7001, user_id))
    conn.execute("DELETE FROM board_visible WHERE user_id = %s", (user_id,))
    conn.execute(
        "INSERT INTO board_visible (user_id, job_id, computed_at) "
        "SELECT %s, unnest(%s::bigint[]), now()",
        (user_id, ids),
    )


def _physical(uid: int) -> dict[int, tuple[str, str]]:
    rows = db.query(
        "SELECT job_id, ctid::text AS ctid, xmin::text AS xmin FROM board_visible "
        "WHERE user_id = %s",
        (uid,),
    )
    return {r["job_id"]: (r["ctid"], r["xmin"]) for r in rows}


def _stored(uid: int) -> set[int]:
    return set(_physical(uid))


def _board(f, *, n: int = 3) -> tuple[int, list[int]]:
    uid = f.make_user()
    jobs = []
    for i in range(n):
        url = f"https://x.test/diff-{uid}-{i}"
        jobs.append(_insert_job("src-diff", url))
        _pass_closed(url)
    _subscribe(uid, "src-diff")
    return uid, jobs


def _committed_now() -> object:
    return db.query_one("SELECT now() AS at")["at"]


def test_an_unchanged_recompute_writes_no_rows_and_advances_computed_at(f):
    uid, jobs = _board(f)
    assert visibility.recompute(uid) == 3
    before = _physical(uid)
    first = visibility.computed_at(uid)
    assert set(before) == set(jobs)

    assert visibility.recompute(uid) == 3

    assert _physical(uid) == before, "an unchanged row was rewritten"
    second = visibility.computed_at(uid)
    assert first is not None and second is not None and second > first


def test_added_and_removed_rows_are_written_and_unchanged_rows_are_not(f):
    uid, jobs = _board(f)
    url = f"https://x.test/diff-{uid}-gone"
    gone = _insert_job("src-diff-gone", url)
    _pass_closed(url)
    _subscribe(uid, "src-diff-gone")
    visibility.recompute(uid)
    before = _physical(uid)
    assert set(before) == {*jobs, gone}

    db.execute(
        "DELETE FROM user_sources WHERE user_id = %s AND source = %s", (uid, "src-diff-gone")
    )
    url = f"https://x.test/diff-{uid}-new"
    new = _insert_job("src-diff", url)
    _pass_closed(url)
    visibility.recompute(uid)

    after = _physical(uid)
    assert set(after) == {*jobs, new} == set(visibility.member_ids(uid))
    assert {j: after[j] for j in jobs} == {j: before[j] for j in jobs}


def test_an_emptied_board_reads_as_never_computed_as_before(f):
    uid, _ = _board(f)
    visibility.recompute(uid)
    db.execute("DELETE FROM user_sources WHERE user_id = %s", (uid,))
    assert visibility.recompute(uid) == 0
    assert _stored(uid) == set()
    assert visibility.computed_at(uid) is None


def test_concurrent_recomputes_end_consistent(f):
    uid, jobs = _board(f)
    visibility.recompute(uid)
    url = f"https://x.test/diff-{uid}-race"
    added = _insert_job("src-diff", url)
    _pass_closed(url)

    errors: list[Exception] = []

    def run() -> None:
        try:
            visibility.recompute(uid)
        except Exception as e:
            errors.append(e)

    # Hold the person's lock so both recomputes queue behind it and race for it.
    with pool.connection() as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s, %s)", (7001, uid))
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        holder.commit()
    for t in threads:
        t.join(30)
    assert not errors
    assert _stored(uid) == {*jobs, added} == set(visibility.member_ids(uid))


def test_a_recompute_whose_transaction_began_first_but_wrote_last_wins_as_before(f):
    """The lock is taken inside the transaction, so a recompute can begin
    before another and write after it. The old writer's rows then carry the
    later writer's now(), which is the earlier timestamp; computed_at said
    so, and still must."""
    uid, jobs = _board(f)
    ids = visibility.member_ids(uid)
    url = f"https://x.test/diff-{uid}-late"
    _pass_closed(url)
    later = _insert_job("src-diff", url)

    with pool.connection() as slow:
        began = slow.execute("SELECT now() AS at").fetchone()["at"]
        visibility.recompute(uid)  # starts after, commits first
        visibility.store(slow, uid, ids)
    assert visibility.computed_at(uid) == began
    assert _stored(uid) == set(jobs)
    assert later not in _stored(uid)
    _assert_populations_agree(uid)


def _assert_populations_agree(uid: int) -> None:
    row = populations.read(user_ids=[uid], sources=[])[0]  # the global row
    at = visibility.computed_at(uid)
    assert row.visibility_computed_at_min == at
    assert row.visibility_computed_at_max == at


def test_old_and_new_writers_coexist(f):
    uid, jobs = _board(f)
    url = f"https://x.test/diff-{uid}-mixed"
    extra = _insert_job("src-diff", url)
    _pass_closed(url)

    # new, then old: the old writer's now() is what computed_at says.
    visibility.recompute(uid)
    with pool.connection() as conn:
        old_at = conn.execute("SELECT now() AS at").fetchone()["at"]
        _old_recompute(conn, uid, jobs)
    assert _stored(uid) == set(jobs)
    assert visibility.computed_at(uid) == old_at
    _assert_populations_agree(uid)

    # old, then new: the new writer's now() wins, and the rows the old writer
    # left are diffed rather than rewritten.
    before = _physical(uid)
    with pool.connection() as conn:
        new_at = conn.execute("SELECT now() AS at").fetchone()["at"]
        visibility.store(conn, uid, [*jobs, extra])
    after = _physical(uid)
    assert set(after) == {*jobs, extra}
    assert {j: after[j] for j in jobs} == before
    assert visibility.computed_at(uid) == new_at
    _assert_populations_agree(uid)

    # and old again on top of new.
    with pool.connection() as conn:
        old_at = conn.execute("SELECT now() AS at").fetchone()["at"]
        _old_recompute(conn, uid, jobs)
    assert visibility.computed_at(uid) == old_at
    _assert_populations_agree(uid)
