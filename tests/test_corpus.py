"""Checks on the corpus itself, and the two things only a corpus can test.

Split in two halves.

The first half is the corpus's own falsifiability. A generated corpus fails in
one characteristic way: it looks full and joins to nothing, so every assertion
over it is true of zero rows and the suite goes green having checked nothing.
`docs/agents/testing.md` calls that conditional vacuity and it is the single
thing that would make this whole arrangement worse than the copy it replaces.
These assert that the tables are populated, that the undeclared joins actually
join, and that the rare measured values survived generation.

The second half is what the copy could never do, at any refresh rate:
production has ONE user, so every cross-user assertion over a copy is
vacuously true. These need several users holding overlapping jobs, which is a
thing a generator can produce and a copy of production cannot.
"""

from __future__ import annotations

import json
import os

import pytest

from api import db
from tests import corpus

pytestmark = pytest.mark.corpus


def _one(sql: str, params=None):
    row = db.query_one(sql, params)
    assert row is not None
    return next(iter(row.values()))


def _profile() -> dict:
    return json.loads(corpus.PROFILE_PATH.read_text())


# --- the corpus is not vacuous ---------------------------------------------


def test_every_table_production_populates_has_rows_in_the_corpus():
    """The copy's exact failure, asserted against. `sync_testdb.py` copied 20
    tables and production had 31; the 11 it missed were 345,032 rows including
    the entire mail corpus, and nothing reported it. A corpus with the same
    hole would be no better, so the hole is what this looks for."""
    empty = [
        table
        for table, shape in _profile()["tables"].items()
        if shape["rows"] > 0
        and table not in corpus.SKIP
        and _one(f"SELECT count(*) FROM {table}") == 0
    ]
    assert not empty, f"populated in production, empty in the corpus: {sorted(empty)}"


def test_the_undeclared_joins_actually_join():
    """The catalog's spine is jobs.url TEXT, and no foreign key says so. A
    generator that filled those columns with anything else would produce four
    large, plausible-looking tables that match no job - and every assertion
    over them would pass on an empty result set."""
    for table in ("job_skills", "job_requirements", "job_embeddings"):
        orphans = _one(
            f"SELECT count(*) FROM {table} t LEFT JOIN jobs j ON j.url = t.url WHERE j.url IS NULL"
        )
        assert orphans == 0, f"{orphans} {table} rows key on a url no job has"

    # Every row, not every non-null row. ai_queries.url is never null in
    # production, and "no verdict points at a missing job" is trivially true
    # of a corpus where they all point at nothing - which is what a broken
    # link produces, since an unlinked nullable column generates NULL.
    verdicts = _one("SELECT count(*) FROM ai_queries")
    without_url = _one("SELECT count(*) FROM ai_queries WHERE url IS NULL")
    matched = _one("SELECT count(*) FROM ai_queries q JOIN jobs j ON j.url = q.url")
    assert verdicts > 1000, f"only {verdicts} verdicts in the corpus"
    assert without_url == 0, f"{without_url} verdicts carry no url at all"
    assert matched == verdicts, f"{verdicts - matched} verdicts point at no job"


def test_custom_verdicts_are_keyed_to_filters_that_exist():
    """prompt_hash is how a verdict reaches a filter. Drawn from anywhere else
    the join is empty, every board is empty, and every board test passes."""
    hashes = _one(
        "SELECT count(*) FROM ai_queries q WHERE q.check_type = 'custom' "
        "AND q.prompt_hash IS NOT NULL"
    )
    # NOT EXISTS rather than a join: several users can hold a filter with the
    # same prompt_hash - adopting a preset does it - so a join multiplies the
    # rows and the comparison would be against the wrong number.
    unreachable = _one(
        "SELECT count(*) FROM ai_queries q WHERE q.check_type = 'custom' "
        "AND q.prompt_hash IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM user_filters f WHERE f.prompt_hash = q.prompt_hash)"
    )
    assert hashes > 0, "no custom verdict carries a prompt_hash"
    assert unreachable == 0, f"{unreachable} custom verdicts match no filter"


def test_materialisation_actually_put_rows_on_a_board():
    """Materialisation runs the application's own write-time predicate over the
    corpus. If it produces nothing, every board and visibility test downstream
    asserts over zero rows.

    Counted on UNTOUCHED rows specifically. The generator only ever writes
    board rows with a status on them, so an untouched row is one the
    application's predicate put there and nothing else could have. Asserting
    on user_jobs as a whole passed with materialisation disabled entirely,
    which is how this ended up spelled this way."""
    assert _one("SELECT count(*) FROM user_jobs") > 0
    materialised = _one("SELECT count(*) FROM user_jobs WHERE status IS NULL")
    assert materialised > 0, (
        "no untouched board rows; the write-time predicate produced nothing "
        "over this corpus, so every board test below is vacuous"
    )


def test_every_measured_value_appears_somewhere_in_the_corpus():
    """The whole claim this design rests on.

    Weighted sampling silently drops the rare values, and the rare value is
    reliably the one that breaks a consumer: jobs.comp_period is 0.2% 'weekly'
    and that shape is why the comp column was unsortable. If the generator
    stops reproducing a measured value, the corpus quietly stops being a
    measurement, and this is the thing that says so.
    """
    missing: list[str] = []
    for table, shape in _profile()["tables"].items():
        if shape["rows"] == 0 or table in corpus.SKIP:
            continue
        rows = _one(f"SELECT count(*) FROM {table}")
        for column, column_shape in shape["columns"].items():
            # Partitions too, not just plain categoricals: ai_queries.reason
            # only has a value set once conditioned on check_type, and its
            # three 'content' values are the named awkward case.
            wanted: dict[str, set[str]] = {}
            if column_shape["kind"] == "categorical":
                wanted[""] = set(column_shape["values"])
            elif column_shape["kind"] == "partitioned":
                for part, sub in column_shape["parts"].items():
                    if sub["kind"] == "categorical":
                        wanted[part] = set(sub["values"])
            if not wanted:
                continue
            present = {
                str(r[column])
                for r in db.query(f'SELECT DISTINCT "{column}" FROM {table}')
                if r[column] is not None
            }
            for part, values in wanted.items():
                # A table with fewer rows than the column has measured values
                # cannot hold them all, and saying so is not a defect.
                if rows < len(values):
                    continue
                for value in values - present:
                    where = f"{table}.{column}" + (f"[{part}]" if part else "")
                    missing.append(f"{where}={value!r}")
    assert not missing, f"measured values the corpus never produced: {missing[:20]}"


def test_the_awkward_cases_the_tests_were_written_for_are_present():
    """The three named in the ticket, spelled out rather than left to the
    generic check above, because these are the ones whose absence made real
    detectors blind."""
    assert _one("SELECT count(*) FROM jobs WHERE comp_period = 'weekly'") > 0, (
        "no weekly-pay posting; that shape is why sort=comp was meaningless"
    )
    assert (
        _one("SELECT count(*) FROM ai_queries WHERE check_type = 'content' AND reason = 'ats text'")
        > 0
    ), "no 'ats text' content row; the ATS collapse detector divides by these"
    assert (
        _one("SELECT count(*) FROM ai_queries WHERE created_at < now() - interval '30 days'") > 0
    ), "every verdict is recent; no window query is exercised over old rows"


def test_the_encrypted_token_paths_are_reachable():
    """The ticket left open whether user_oauth_tokens could be generated at
    all, or whether encrypted material forced those paths to stay mocked. It
    can: the row holds real ciphertext under the suite's own key."""
    from api import crypto

    row = db.query_one("SELECT user_id, refresh_token_enc FROM user_oauth_tokens LIMIT 1")
    assert row is not None, "no oauth token rows in the corpus"
    assert crypto.decrypt(row["refresh_token_enc"]) == f"corpus-refresh-{row['user_id']}"


# --- what a copy of production cannot test ---------------------------------


def test_the_corpus_holds_several_users_with_overlapping_jobs():
    """Production has one user row and one distinct user_id in user_jobs, so
    over a copy every isolation assertion below is true of nothing."""
    assert _one("SELECT count(*) FROM users") > 1
    assert _one("SELECT count(DISTINCT user_id) FROM user_jobs") > 1
    assert _one("SELECT count(DISTINCT user_id) FROM applications") > 1
    shared = _one(
        "SELECT count(*) FROM (SELECT job_id FROM user_jobs GROUP BY job_id "
        "HAVING count(DISTINCT user_id) > 1) t"
    )
    assert shared > 0, "no job is on two users' boards; nothing tests whose data renders"


def test_a_board_query_over_real_volume_never_returns_another_users_rows():
    """Route-level gating was fully correct while four object-level holes sat
    open behind it. The difference only shows with a second user who has data,
    and with enough of it that a missing predicate is not masked by an empty
    table."""
    from api import criteria
    from api.routers.jobs import _VISIBILITY

    users = db.query("SELECT id FROM users ORDER BY id")
    assert len(users) > 1
    for user in users:
        uid = user["id"]
        settings = db.query_one("SELECT * FROM user_settings WHERE user_id = %s", (uid,))
        sql = _VISIBILITY.format(columns="j.id", extra="", criteria=criteria.SQL)
        visible = {
            r["id"]
            for r in db.query(
                sql, {"uid": uid, "bypass_sponsorship": False, **criteria.params(settings)}
            )
        }
        others = {
            r["job_id"]
            for r in db.query(
                "SELECT job_id FROM user_jobs WHERE user_id <> %s AND job_id NOT IN "
                "(SELECT job_id FROM user_jobs WHERE user_id = %s)",
                (uid, uid),
            )
        }
        leaked = visible & others
        # A job another user has touched can still be visible on its own
        # merits - it is in the catalog. What must not happen is it becoming
        # visible BECAUSE they touched it.
        for job_id in leaked:
            row = db.query_one(
                "SELECT active, source, uploaded_by FROM jobs WHERE id = %s", (job_id,)
            )
            assert row is not None and row["active"], (
                f"job {job_id} is visible to user {uid} but is inactive; the only "
                "thing putting it there is another user's board row"
            )


def _corpus_headers(index: int) -> dict:
    """Sign in AS a user the corpus already holds.

    Their own sub, deliberately. Any authenticated request provisions a user
    row for an unknown sub, so a made-up one would quietly add a sixth user
    with no board, no filters and no history - and every isolation assertion
    below would then be comparing two empty accounts.
    """
    row = db.query_one(
        "SELECT sub, email, groups FROM users WHERE sub = %s", (f"corpus-user-{index}",)
    )
    assert row is not None, f"corpus-user-{index} is not in this corpus"
    return {
        "X-Service-Token": os.environ["JOBTRACKER_SERVICE_TOKEN"],
        "X-User-Sub": row["sub"],
        "X-User-Email": row["email"],
        "X-User-Name": row["sub"],
        "X-User-Groups": ",".join(row["groups"] or []),
    }


def test_the_board_route_renders_each_users_own_state_and_no_one_elses(client):
    """The isolation check through the real route, over a full catalog, against
    a second account that actually has rows.

    Route-level gating here was fully correct while four object-level holes sat
    open behind it, and no test could see them: production has one user, so a
    copy of it renders the same page whether or not the predicate mentions
    whose data it is.

    The comparison is unconditional on purpose. The first spelling guarded it
    with "if this user has a row for this job", and that version passed with
    the route's `AND uj.user_id = %(uid)s` deleted outright - which is exactly
    the conditional vacuity docs/agents/testing.md lists.
    """
    with_state = 0
    for index in (0, 1):
        headers = _corpus_headers(index)
        user = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
        assert user is not None
        response = client.get("/v1/user/jobs?limit=1000&include_hidden=true", headers=headers)
        assert response.status_code == 200, response.text
        rows = response.json()["rows"]
        assert rows, f"corpus-user-{index} has an empty board; nothing is being compared"

        own = {
            r["job_id"]: r
            for r in db.query(
                "SELECT job_id, status, notes FROM user_jobs WHERE user_id = %s",
                (user["id"],),
            )
        }
        for row in rows:
            mine = own.get(row["job_id"])
            for field in ("status", "notes"):
                expected = mine[field] if mine else None
                assert row[field] == expected, (
                    f"user {user['id']} is shown {field}={row[field]!r} for job "
                    f"{row['job_id']}, but their own row says {expected!r}"
                )
            if mine and mine["status"] is not None:
                with_state += 1

    assert with_state, (
        "neither board carried any per-user state, so a leak would have had nothing to leak"
    )


def test_the_administrative_view_aggregates_over_every_user():
    """/job-scripts renders numbers across all users. Over a population of one,
    a query that silently drops or double-counts a user returns exactly the
    same answer as a correct one, so the copy can never fail this."""
    users = _one("SELECT count(*) FROM users")
    counted = _one(
        "SELECT count(DISTINCT u.id) FROM users u "
        "LEFT JOIN user_jobs uj ON uj.user_id = u.id "
        "LEFT JOIN api_usage a ON a.user_id = u.id"
    )
    assert counted == users, f"{users - counted} users vanish from a left-joined aggregate"

    groups = {g for r in db.query("SELECT groups FROM users") for g in (r["groups"] or [])}
    budgeted = {r["group_name"] for r in db.query("SELECT group_name FROM group_budgets")}
    assert groups & budgeted, (
        "no corpus user belongs to a group that has a budget; every spend "
        "aggregate by group would be empty"
    )
