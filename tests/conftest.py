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

db.init_schema()
import core.store  # noqa: E402,F401  (import triggers ai_queries creation)


def pytest_sessionfinish(session, exitstatus) -> None:
    if _stop_scratch_pg is not None:
        _stop_scratch_pg()


_MUTABLE_TABLES = [
    "tasks",
    "jobs",
    "user_jobs",
    "users",
    "user_filters",
    "user_settings",
    "user_sources",
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
