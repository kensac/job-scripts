from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

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
    "    export TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/jobtracker_test\n"
    "Prod and CI both run pgvector; a local install without it is the odd one out."
)


def _provision() -> None:
    """Migrate, and translate the one failure that has a non-obvious cause.

    The scratch server is built from whatever local install `pg_config` points
    at, which for most machines is a homebrew Postgres with no vector
    extension in its sharedir. `CREATE EXTENSION vector` then fails several
    frames deep at import time, naming neither pgvector nor the way out.

    Deliberately reactive rather than a pre-flight check: a guard that refuses
    to start whenever pgvector is absent would break every session's suite
    immediately, for a dependency no migration has needed yet.
    """
    import psycopg

    try:
        db.init_schema()
    except psycopg.errors.UndefinedFile as exc:  # missing extension control file
        if "vector" not in str(exc).lower():
            raise
        raise RuntimeError(_PGVECTOR_HELP) from exc


if not _schema_already_provisioned():
    _provision()
import core.store  # noqa: E402,F401  (import triggers ai_queries creation)


def pytest_sessionfinish(session, exitstatus) -> None:
    if _stop_scratch_pg is not None:
        _stop_scratch_pg()


_MUTABLE_TABLES = [
    "tasks",
    "jobs",
    "job_requirements",
    "job_skills",
    "user_jobs",
    "users",
    "user_filters",
    "user_settings",
    "user_sources",
    "user_oauth_tokens",
    "ai_queries",
    "api_usage",
    "reports",
    "source_requests",
    "sources",
    "source_groups",
    "group_budgets",
    "filter_presets",
    "app_config",
]


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
def _clean_db(request):
    """Isolate unit tests by truncating between them.

    Integration tests are exempt, and that exemption is load-bearing: they run
    against a synced copy of production, so truncating first would delete the
    only thing they are there to inspect. The guard test in the integration
    suite exists because this fixture silently did exactly that.
    """
    if request.node.get_closest_marker("integration"):
        yield
        return
    db.execute(f"TRUNCATE TABLE {', '.join(_MUTABLE_TABLES)} RESTART IDENTITY CASCADE")
    _reseed()
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from api.app import app

    return TestClient(app)


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
