"""Analytics name three populations instead of one overloaded count (phase 2b).

acted_on: postings the person acted on. working_set: postings their filters
picked. visible: postings they can see. The fixture puts each posting in a
different combination, so a count that reads the wrong table disagrees.
"""

from __future__ import annotations

import pytest

from api import db
from api.board import populations
from api.board.person_state import PERSON_STATE


def _seed(f, uid: int) -> None:
    source = f.make_source("pop-src")
    picked = f.make_job(source=source)  # filters picked it; on the computed board
    applied = f.make_job(source=source)  # the person applied; no filter picked it
    hidden = f.make_job(source=source)  # the person hid it
    left = f.make_job(source=source)  # a machine row whose posting left the board
    db.execute("INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s)", (uid, picked))
    db.execute("INSERT INTO user_job_working_set (user_id, job_id) VALUES (%s, %s)", (uid, picked))
    db.execute("INSERT INTO board_visible (user_id, job_id) VALUES (%s, %s)", (uid, picked))
    f.make_board_row(uid, applied, status="Applied")
    db.execute(
        "INSERT INTO user_jobs (user_id, job_id, hidden, person_touched_at) "
        "VALUES (%s, %s, true, now())",
        (uid, hidden),
    )
    db.execute("INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s)", (uid, left))


def test_person_stats_name_visible_and_acted_on_beside_the_old_counts(client, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    _seed(f, uid)

    totals = client.get("/v1/user/stats", headers=user_headers).json()["totals"]

    assert totals["visible"] == 2, "picked (on the board) and applied (acted on), not hidden"
    assert totals["acted_on"] == 2, "applied and hidden; machine rows are not person state"
    assert totals["tracked"] == 4, "the old overloaded count is kept until the frontend moves"


def test_admin_surfaces_name_all_three(client, admin_headers, user_headers, f):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    _seed(f, uid)

    users = client.get("/v1/admin/users", headers=admin_headers).json()["users"]
    person = next(u for u in users if u["id"] == uid)
    assert (person["acted_on"], person["working_set"], person["visible"]) == (2, 1, 2)
    assert person["board_rows"] == 4

    row = client.get("/v1/analytics/sources/pop-src", headers=admin_headers).json()["row"]
    board = row["board_yield"]
    assert (board["acted_on"], board["working_set"], board["visible"]) == (2, 1, 1), (
        "per source, visible is the computed board"
    )


@pytest.mark.corpus
def test_the_old_count_is_acted_on_plus_working_set_rows_on_the_corpus():
    """The overloaded user_jobs count splits exactly into person state and
    machine rows the working set also holds. That is what lets the old count
    go: nothing in it is outside the two labelled populations. On production
    the same check is the PR's decision-1 SQL, after the split backfill."""
    rows = db.query(
        f"""
        SELECT u.id, (SELECT count(*) FROM user_jobs uj WHERE uj.user_id = u.id) AS board_rows,
               {populations.per_user_counts("u.id")},
               (SELECT count(*) FROM user_jobs uj WHERE uj.user_id = u.id AND NOT {PERSON_STATE}
                  AND NOT EXISTS (SELECT 1 FROM user_job_working_set ws
                                  WHERE ws.user_id = uj.user_id AND ws.job_id = uj.job_id))
                   AS outside_both,
               (SELECT count(*) FROM user_jobs uj WHERE uj.user_id = u.id AND NOT {PERSON_STATE})
                   AS machine
        FROM users u ORDER BY u.id
        """
    )
    assert any(r["board_rows"] for r in rows), "the corpus must hold board rows"
    for r in rows:
        assert r["outside_both"] == 0, r
        assert r["board_rows"] == r["acted_on"] + r["machine"], r
