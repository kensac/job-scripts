from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Must be set before api.auth / api.crypto / api.metrics are imported anywhere.
os.environ.setdefault("JOBTRACKER_SERVICE_TOKEN", "test-service-token")
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JOBTRACKER_METRICS_PORT", "19391")

_stop_scratch_pg = None


def _run(cmd: list) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stderr}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pg_bindir() -> str:
    if not shutil.which("pg_config"):
        raise RuntimeError(
            "pg_config not found on PATH; install postgresql or set TEST_DATABASE_URL"
        )
    out = subprocess.run(["pg_config", "--bindir"], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _start_scratch_postgres() -> str:
    global _stop_scratch_pg
    bindir = _pg_bindir()
    scratch_dir = Path(tempfile.mkdtemp(prefix="jobtracker-test-pg-"))
    data_dir = scratch_dir / "data"
    port = _free_port()
    _run([f"{bindir}/initdb", "-D", str(data_dir), "-U", "postgres", "--auth=trust", "-E", "UTF8"])
    # -l redirects postgres's own stdout/stderr to a file; without it, the
    # daemonized postmaster inherits our pipe fds and subprocess.run() (which
    # waits for the pipes to close) hangs forever since postgres never exits.
    _run(
        [
            f"{bindir}/pg_ctl",
            "-D",
            str(data_dir),
            "-w",
            "-l",
            str(scratch_dir / "postgres.log"),
            "-o",
            f"-p {port} -c unix_socket_directories=''",
            "start",
        ]
    )
    _run(
        [
            f"{bindir}/createdb",
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-U",
            "postgres",
            "jobtracker_test",
        ]
    )

    def _stop() -> None:
        subprocess.run(
            [f"{bindir}/pg_ctl", "-D", str(data_dir), "-m", "fast", "stop"], capture_output=True
        )
        shutil.rmtree(scratch_dir, ignore_errors=True)

    _stop_scratch_pg = _stop
    return f"postgresql://postgres@127.0.0.1:{port}/jobtracker_test"


def _assert_disposable(url: str) -> str:
    """Refuse to run against a database whose name does not mark it disposable.

    The autouse fixture below TRUNCATEs every mutable table between tests. A
    mistyped TEST_DATABASE_URL pointed at production would therefore erase it,
    with no confirmation step anywhere. The database name is the one thing a
    caller cannot get wrong by accident, so it is what we gate on.
    """
    from urllib.parse import urlparse

    name = (urlparse(url).path or "").lstrip("/")
    if not (name.endswith(("_test", "_ci")) or name.startswith("test_")):
        raise RuntimeError(
            f"refusing to run tests against database {name!r}: the test suite "
            "truncates every table between tests. Name it *_test or *_ci."
        )
    return url


os.environ["DATABASE_URL"] = (
    _assert_disposable(os.environ["TEST_DATABASE_URL"])
    if os.environ.get("TEST_DATABASE_URL")
    else _start_scratch_postgres()
)

from api import db  # noqa: E402  (import only after DATABASE_URL is set)

# One pytest process per database, enforced rather than assumed.
#
# The autouse fixture below TRUNCATEs every mutable table between tests. Two
# runs sharing a database therefore delete each other's rows mid-test, and the
# symptom is never a truncation error - it is a foreign key violation on an id
# that existed when the row was read and did not when it was written, with the
# id varying run to run. Reproduced deterministically: two concurrent runs of
# tests/test_mail_match_task.py against one database produced 1 and 3 failures
# with "Key (user_id)=(4) is not present in table users" and "Key (job_id)=(5)
# is not present in table jobs". Each run passes alone.
#
# That looks exactly like a bug in whatever code the losing test happened to
# be exercising, which is what makes it expensive: the reader debugs their own
# change. The container's PORT is already derived from the checkout so
# parallel worktrees cannot collide, but two runs in ONE checkout still can -
# a second terminal, or a full-suite run started while another is going.
#
# An advisory lock is the cheapest thing that converts silent corruption into
# a sentence. It is per-database and released when this connection closes, so
# a crashed run does not wedge the next one. The key is derived from a fixed
# string rather than picked, so it cannot collide with db.py's schema lock.
_EXCLUSIVE_KEY = int.from_bytes(
    hashlib.sha256(b"jobtracker-pytest-exclusive").digest()[:8], "big", signed=True
)


def _claim_database_exclusively() -> None:
    conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
    row = conn.execute("SELECT pg_try_advisory_lock(%s) AS got", (_EXCLUSIVE_KEY,)).fetchone()
    if not (row and row[0]):
        conn.close()
        raise RuntimeError(
            "another pytest run is already using this database "
            f"({urlparse(os.environ['DATABASE_URL']).path.lstrip('/')}).\n"
            "Two runs sharing one database TRUNCATE each other's rows between "
            "tests, which surfaces as foreign key violations on ids that "
            "vanished mid-test - in whichever test happened to lose the race, "
            "not in the one that caused it.\n"
            "Wait for the other run, or point TEST_DATABASE_URL at a different "
            "database."
        )
    # Held for the life of the process; never closed explicitly, so the lock
    # outlives every fixture and is released only when this process exits.
    globals()["_exclusive_conn"] = conn


_claim_database_exclusively()


def _schema_already_provisioned() -> bool:
    """True when the target database already has the schema, which is the case
    for an integration run against a synced copy. The credential there is
    read-only on purpose, so attempting to migrate or seed would fail - and
    should, rather than the role being widened to make a test pass."""
    row = db.query_one("SELECT to_regclass('public.alembic_version') AS t")
    if not (row and row["t"]):
        return False
    version = db.query_one("SELECT count(*) AS c FROM alembic_version")
    return bool(version and version["c"])


_PGVECTOR_HELP = (
    "This Postgres has no pgvector, and a migration needs it.\n"
    "Point the suite at one that does:\n"
    "    make testdb-up\n"
    # Not restated here. The container's port is derived from the checkout path
    # so that parallel worktrees cannot share one database, which means this
    # file cannot know it - and a second hardcoded copy would be wrong for
    # every checkout but one.
    '    # then export the line it prints, or: eval "$(make testdb-url)"\n'
    "Prod and CI both run pgvector; a local install without it is the odd one out."
)


def _provision() -> None:
    """Migrate, and translate the one failure that has a non-obvious cause.

    The scratch server is built from whatever local install `pg_config` points
    at, which for most machines is a homebrew Postgres with no vector
    extension in its sharedir. `CREATE EXTENSION vector` then fails several
    frames deep at import time, naming neither pgvector nor the way out.

    The cause is matched on the CHAIN, not on the top-level type. Alembic runs
    through SQLAlchemy, so psycopg's UndefinedFile arrives wrapped in
    sqlalchemy.exc.OperationalError - the first version of this guard caught
    the psycopg class directly and therefore never fired once. Matching the
    extension name in the message text is cruder and works regardless of how
    many layers wrap it.

    Deliberately reactive rather than a pre-flight check: a guard that refused
    to start whenever pgvector was absent would break every session's suite
    immediately, for a dependency no migration had needed yet.
    """
    try:
        db.init_schema()
    except Exception as exc:
        text = " ".join(str(e) for e in _causes(exc))
        if "vector.control" not in text and 'extension "vector"' not in text:
            raise
        raise RuntimeError(_PGVECTOR_HELP) from exc


def _causes(exc: BaseException) -> list[BaseException]:
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
    return chain


if not _schema_already_provisioned():
    _provision()


def pytest_sessionfinish(session, exitstatus) -> None:
    if _stop_scratch_pg is not None:
        _stop_scratch_pg()


# Tables that must SURVIVE a truncate. Everything else is derived, because a
# hand-maintained enumeration silently rots: ai_batches was missing from the
# old list and leaked rows between tests until a unique constraint failed, and
# all five email tables were missing too - their tests only passed because
# they happened to use unique ids.
#
# Listing the exceptions instead of the members means adding a table never
# requires remembering to add it here, and forgetting fails loudly (a table
# that should have persisted gets emptied) rather than silently (state leaks
# between tests until something collides).
_PERSISTENT_TABLES = frozenset({"alembic_version"})


def _mutable_tables() -> list[str]:
    rows = db.query(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    return [r["tablename"] for r in rows if r["tablename"] not in _PERSISTENT_TABLES]


# Whether this database was a synced copy of production when the run started.
#
# Read ONCE, at import, because the answer must not depend on what the run has
# already done to the database. `_mutable_tables()` excludes only
# alembic_version, so app_config - where sync_testdb.py stamps
# `testdb_synced_at` - is emptied by the first unmarked test, and `_reseed()`
# does not put it back.
#
# Asked live, that made the whole arrangement fail silently. On a machine whose
# TEST_DATABASE_URL held a synced copy, `pytest tests` truncated the copy in its
# first test file, and from then on every `integration` test skipped with "this
# database holds the generated corpus or nothing": a green run that checked
# nothing, which is precisely the false negative _clean_db exists to prevent.
# The guard in corpus.build() reads the same stamp, so it could not fire either.
_SYNCED_AT = db.query_one("SELECT value FROM app_config WHERE key = 'testdb_synced_at'")


def _reseed() -> None:
    db._seed_sources()
    for group, tokens in db._GROUP_BUDGET_SEED:
        db.execute(
            "INSERT INTO group_budgets (group_name, weekly_token_budget) "
            "VALUES (%s, %s) ON CONFLICT (group_name) DO NOTHING",
            (group, tokens),
        )
    for key, value in db._APP_CONFIG_SEED:
        db.execute(
            "INSERT INTO app_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            (key, db.jsonb(value)),
        )


@pytest.fixture(autouse=True)
def _no_network_static_fetch(monkeypatch):
    """The browserless fetch tier is seeded on, and it is a real HTTP client.
    A test that stubs the browser and lets the tier run would fetch the
    posting for real (one did, and got a real page). Every test starts with
    the tier failing closed, so the browser stub is reached exactly as before;
    a test of the tier itself replaces this with its own stub."""
    import curl_cffi.requests as cffi_requests

    def refuse(url, **kw):
        raise RuntimeError("tests do not reach the network")

    monkeypatch.setattr(cffi_requests, "get", refuse)


@pytest.fixture(autouse=True)
def _clean_db(request):
    """Give each test the database its marker asks for.

    Three populations, and the difference between them is what gets truncated:

    unmarked  a truncated database it fills itself. 632 tests, unchanged.
    corpus    the generated corpus from tests/corpus.py, built from a
              measurement of production. Not truncated, or there would be
              nothing to read; rebuilt lazily when an unmarked test has
              emptied it.
    integration  a synced copy of REAL production. Skipped unless the database
              actually holds one, so a full `pytest tests` on CI - which has
              no production credential and must not have one - skips them
              rather than failing or, worse, passing against empty tables.
    """
    if request.node.get_closest_marker("integration"):
        if not _SYNCED_AT:
            pytest.skip(
                "needs a synced copy of production (make testdb-sync); "
                "this database holds the generated corpus or nothing"
            )
        yield
        return

    if request.node.get_closest_marker("corpus"):
        from tests import corpus

        if not corpus.is_present():
            corpus.build()
        yield
        return

    if _SYNCED_AT:
        # An unmarked test truncates, and this database is a copy of
        # production. Refuse rather than delete it: the copy takes minutes to
        # cut, and the run that destroyed it would go green while every test
        # that wanted it skipped.
        raise RuntimeError(
            f"{urlparse(os.environ['DATABASE_URL']).path.lstrip('/')!r} holds a synced "
            "copy of production, and this test truncates every table.\n"
            "Run only the tests that want the copy:\n"
            "    make integration\n"
            "or point TEST_DATABASE_URL at a scratch database for the rest."
        )
    db.execute(f"TRUNCATE TABLE {', '.join(_mutable_tables())} RESTART IDENTITY CASCADE")
    _reseed()
    yield


@pytest.fixture
def client(request):
    """The API, with the board's membership recomputed before any board or
    per-object read. In production a worker recomputes a person's board
    within a minute of a preference write and every board_refresh_minutes;
    the tests assert what the predicate admits, not the worker's timing, so
    the read itself triggers the recompute. A test that is ABOUT the timing
    opts out with the no_board_recompute marker."""
    from fastapi.testclient import TestClient

    from api import visibility
    from api.app import app

    tc = TestClient(app)
    if request.node.get_closest_marker("no_board_recompute"):
        return tc
    original = tc.request

    def recomputing(method, url, *args, **kwargs):
        headers = kwargs.get("headers") or {}
        path = str(url)
        sub = headers.get("X-User-Sub")
        if sub and (path.startswith("/v1/user/jobs") or path.startswith("/v1/requirements")):
            row = db.query_one("SELECT id FROM users WHERE sub = %s", (sub,))
            if row:
                visibility.recompute(row["id"])
        return original(method, url, *args, **kwargs)

    tc.request = recomputing  # type: ignore[method-assign]
    return tc


SERVICE_TOKEN = os.environ["JOBTRACKER_SERVICE_TOKEN"]


def _auth_headers(sub: str, email: str, groups: list) -> dict:
    return {
        "X-Service-Token": SERVICE_TOKEN,
        "X-User-Sub": sub,
        "X-User-Email": email,
        "X-User-Name": sub,
        "X-User-Groups": ",".join(groups),
    }


@pytest.fixture
def user_headers(client) -> dict:
    headers = _auth_headers("test-user", "user@example.com", ["jobtracker-users-internal"])
    resp = client.post("/v1/users/bootstrap", headers=headers)
    assert resp.status_code == 200, resp.text
    return headers


@pytest.fixture
def other_user_headers(client) -> dict:
    """A second, unrelated signed-in user. Every object-level authorization
    test needs one, and the suite had none - which is why four cross-user
    holes sat open behind fully-correct route-level gating."""
    headers = _auth_headers("test-user-2", "user2@example.com", ["jobtracker-users-internal"])
    resp = client.post("/v1/users/bootstrap", headers=headers)
    assert resp.status_code == 200, resp.text
    return headers


@pytest.fixture
def admin_headers(client) -> dict:
    headers = _auth_headers("test-admin", "admin@example.com", ["infra-admins"])
    resp = client.post("/v1/users/bootstrap", headers=headers)
    assert resp.status_code == 200, resp.text
    return headers


@pytest.fixture
def f():
    """The row builders, as a fixture so a test reads `f.make_ready_job(...)`
    and does not need an import line per helper."""
    from tests import factories

    return factories
