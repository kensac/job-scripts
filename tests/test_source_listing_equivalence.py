"""The source listings were rewritten for speed and must answer exactly as
before: same rows, same values, same order. Each test runs the SQL the
endpoint used before the rewrite against a fixture holding the awkward cases,
and compares it with what the endpoint now serves.

The fixture holds: bundles that overlap, an empty bundle, an inactive bundle,
a bundle naming a source twice and naming a source that does not exist,
sources in no bundle, inactive sources one person still holds, ingest tasks
whose created_at ties or runs against id order, a source never ingested, and
ingest tasks for a source that no longer exists."""

from __future__ import annotations

from api import db
from api.routers.source_admin import SourceLedger, SourceName
from api.routers.sources import Source
from core.fetching import boards

# The queries as they stood before the rewrite (origin/main 845624c).
_OLD_USER_SOURCES = """
    SELECT s.name, s.listings_url, s.description, s.company, s.active,
           us.user_id IS NOT NULL AS enabled,
           COALESCE((SELECT array_agg(g.name ORDER BY g.name) FROM source_groups g
                     WHERE g.active AND s.name = ANY(g.members)), '{}') AS groups
    FROM sources s
    LEFT JOIN user_sources us ON us.source = s.name AND us.user_id = %s
    WHERE s.active OR us.user_id IS NOT NULL
    ORDER BY s.name
"""

_OLD_NAMES = """
    SELECT s.name, s.listings_url, s.active,
           COALESCE((SELECT array_agg(g.name ORDER BY g.name) FROM source_groups g
                     WHERE s.name = ANY(g.members)), '{}') AS groups
    FROM sources s ORDER BY s.active DESC, s.name
"""

_OLD_LEDGER = """
    WITH catalog AS (
        SELECT source, COUNT(*) AS jobs, MAX(created_at) AS last_new_posting_at
        FROM jobs GROUP BY source
    ),
    subscribers AS (
        SELECT source, COUNT(*) AS subscribers FROM user_sources GROUP BY source
    ),
    bundles AS (
        SELECT m AS source, array_agg(g.name ORDER BY g.name) AS groups
        FROM source_groups g, unnest(g.members) AS m GROUP BY m
    ),
    last_ingest AS (
        SELECT DISTINCT ON (payload->>'source')
               payload->>'source' AS source, status, finished_at, error
        FROM tasks WHERE kind = 'ingest_source'
        ORDER BY payload->>'source', id DESC
    )
    SELECT s.name, s.listings_url, s.description, s.active, s.created_at,
           s.company, s.ingest_interval_hours,
           COALESCE(b.groups, '{}') AS groups,
           COALESCE(c.jobs, 0) AS jobs,
           COALESCE(u.subscribers, 0) AS subscribers,
           li.status AS last_ingest_status,
           li.finished_at AS last_ingest_at,
           li.error AS last_ingest_error,
           c.last_new_posting_at
    FROM sources s
    LEFT JOIN catalog c ON c.source = s.name
    LEFT JOIN subscribers u ON u.source = s.name
    LEFT JOIN bundles b ON b.source = s.name
    LEFT JOIN last_ingest li ON li.source = s.name
    ORDER BY s.active DESC, s.name
"""


def _fixture(f) -> None:
    for name, active in (
        ("a_both", True),
        ("b_one", True),
        ("c_none", True),
        ("d_off_held", False),
        ("e_off", False),
        ("f_dup", True),
        ("g_never", True),
    ):
        f.make_source(name, active=active)
    db.execute(
        """
        INSERT INTO source_groups (name, members, active) VALUES
          ('zeta', ARRAY['a_both', 'b_one', 'd_off_held'], true),
          ('alpha', ARRAY['a_both', 'e_off', 'ghost'], true),
          ('empty', '{}', true),
          ('retired', ARRAY['a_both', 'c_none'], false),
          ('twice', ARRAY['f_dup', 'f_dup', 'a_both'], true)
        """
    )
    # Ties: equal created_at, so only id decides the last one. Inversions:
    # the higher id has the EARLIER created_at, so an ordering by
    # created_at would pick a different row. A source never ingested, and
    # tasks for a source that no longer exists.
    db.execute(
        """
        INSERT INTO tasks (kind, payload, status, created_at, finished_at, error) VALUES
          ('ingest_source', '{"source": "a_both"}', 'done',
           '2026-10-01 10:00+00', '2026-10-01 10:05+00', NULL),
          ('ingest_source', '{"source": "a_both"}', 'failed',
           '2026-10-01 10:00+00', '2026-10-01 10:06+00', 'tie, higher id'),
          ('ingest_source', '{"source": "b_one"}', 'done',
           '2026-10-02 09:00+00', '2026-10-02 09:01+00', NULL),
          ('ingest_source', '{"source": "b_one"}', 'failed',
           '2026-10-01 09:00+00', NULL, 'earlier created_at, higher id'),
          ('ingest_source', '{"source": "c_none"}', 'pending', now(), NULL, NULL),
          ('ingest_source', '{"source": "d_off_held"}', 'done', now(), now(), NULL),
          ('ingest_source', '{"source": "gone"}', 'done', now(), now(), NULL),
          ('run_managed_board', '{"source": "g_never"}', 'done', now(), now(), NULL)
        """
    )
    for source, n in (("a_both", 3), ("c_none", 1), ("e_off", 2)):
        for _ in range(n):
            f.make_job(source=source)


def _user_id(sub: str) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (sub,))
    assert row
    return row["id"]


def test_user_sources_answer_as_before(client, user_headers, f):
    _fixture(f)
    uid = _user_id("test-user")
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, 'a_both'), (%s, 'd_off_held')",
        (uid, uid),
    )
    old = db.query(_OLD_USER_SOURCES, (uid,))
    # The fixture must exercise what it claims to, or equality is vacuous.
    by_name = {r["name"]: r["groups"] for r in old}
    assert by_name["a_both"] == ["alpha", "twice", "zeta"]
    assert by_name["f_dup"] == ["twice"] and by_name["c_none"] == []
    assert "d_off_held" in by_name and "e_off" not in by_name

    want = [Source(**r, kind=boards.kind(r["listings_url"])).model_dump(mode="json") for r in old]
    got = client.get("/v1/sources", headers=user_headers).json()["sources"]
    assert got == want


def test_admin_names_answer_as_before(client, admin_headers, f):
    _fixture(f)
    old = db.query(_OLD_NAMES)
    by_name = {r["name"]: r["groups"] for r in old}
    assert by_name["a_both"] == ["alpha", "retired", "twice", "zeta"]
    assert by_name["f_dup"] == ["twice"] and by_name["g_never"] == []

    want = [
        SourceName(
            name=r["name"],
            active=r["active"],
            groups=r["groups"],
            kind=boards.kind(r["listings_url"]),
        ).model_dump(mode="json")
        for r in old
    ]
    got = client.get("/v1/admin/sources?shape=names", headers=admin_headers).json()["sources"]
    assert got == want


def test_admin_ledger_answers_as_before(client, admin_headers, f):
    _fixture(f)
    old = db.query(_OLD_LEDGER)
    by_name = {r["name"]: r for r in old}
    assert by_name["a_both"]["last_ingest_error"] == "tie, higher id"
    assert by_name["b_one"]["last_ingest_error"] == "earlier created_at, higher id"
    assert by_name["g_never"]["last_ingest_status"] is None
    assert by_name["a_both"]["jobs"] == 3

    got = client.get("/v1/admin/sources", headers=admin_headers).json()["sources"]
    # The in-flight task is read by a query this change did not touch.
    tasks = {g["name"]: g["task"] for g in got}
    want = [
        SourceLedger(
            **r, kind=boards.kind(r["listings_url"]), task=tasks.get(r["name"])
        ).model_dump(mode="json")
        for r in old
    ]
    assert got == want
