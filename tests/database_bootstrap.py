"""Create and validate disposable databases for the test session."""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

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
    """The autouse fixture truncates tables, so validate before opening the pool."""
    from core.disposable_db import require_disposable_dsn

    require_disposable_dsn(url)
    return url


def _worker_database(dsn: str, worker: str) -> str:
    """One database per xdist worker, created here on demand.

    The advisory lock below is what keeps two runs from truncating each
    other's rows, and under `-n` it would simply refuse every worker after
    the first. So each worker gets its own database rather than the lock
    being loosened: the property it protects is the one parallelism needs
    most. Migrating an empty database costs about 1.3 s, which is less than
    the resets a single worker saves in its first few tests.

    The name is derived, not chosen, so it still satisfies the disposable
    rule that anything destructive checks: jobtracker_test -> jobtracker_gw0_test.
    """
    from core.disposable_db import require_disposable_name

    base = str(conninfo_to_dict(dsn).get("dbname") or "")
    for suffix in ("_test", "_ci"):
        if base.endswith(suffix):
            name = f"{base[: -len(suffix)]}_{worker}{suffix}"
            break
    else:
        name = f"{base}_{worker}_test"
    require_disposable_name(name)
    # CREATE DATABASE cannot run inside a transaction, and two workers reach
    # this at the same moment on the first parallel run, so the race is
    # expected rather than guarded against with a lock.
    with psycopg.connect(dsn, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            with contextlib.suppress(psycopg.errors.DuplicateDatabase):
                conn.execute(f'CREATE DATABASE "{name}"')
    # As a URL when it arrived as one: db.py hands this string to SQLAlchemy
    # for the migrations, and SQLAlchemy cannot parse libpq's keyword form.
    parsed = urlparse(dsn)
    if parsed.scheme:
        return urlunparse(parsed._replace(path=f"/{name}"))
    return make_conninfo(dsn, dbname=name)
