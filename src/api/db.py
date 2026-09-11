from __future__ import annotations

import datetime
import decimal
import json
from typing import Any, LiteralString, cast

import dotenv
from psycopg.types.json import Jsonb

from api.config import CONFIG_KEYS

dotenv.load_dotenv()

# The pool and the instrumentation that must precede it live in core/pool.py,
# which core/store.py and core/catalog.py share. See that module for why
# there used to be two.
from core.pool import connection as _connection  # noqa: E402
from core.pool import pool, transaction  # noqa: E402

# Weekly owner-key token budgets by Authentik group. Seeded once with ON
# CONFLICT DO NOTHING so runtime edits via /v1/admin/group-budgets stick;
# groups absent from the table (e.g. jobtracker-users-public) are BYO-only.
_GROUP_BUDGET_SEED = [
    ("infra-admins", None),
    ("jobtracker-users-internal", 5_000_000),
]


_APP_CONFIG_SEED = [(key, spec.default) for key, spec in CONFIG_KEYS.items()]
_APP_CONFIG_DEFAULTS = dict(_APP_CONFIG_SEED)


# One constant key, so every process that does startup DDL queues behind the
# same lock. Nothing else serialises this: on a lockstep roll the three CD runs
# execute in parallel and hetzner recreates api+worker together, so up to four
# processes can enter `alembic upgrade head` within the same minute, from
# different hosts. It has held only because every migration so far was additive
# and the losers of the race landed on idempotent guards.
_SCHEMA_LOCK_KEY = 8_274_113_907_441_002


def init_schema() -> None:
    # Blocking, not try-lock: a worker waiting a few seconds for a peer's
    # migration is correct; skipping it and then running against a
    # half-converted schema is not.
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_KEY,))
        try:
            _migrate()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_KEY,))
    seed_defaults()


def seed_defaults() -> None:
    # Share startup and test defaults. One transaction preserves existing
    # overrides and prevents a failed batch leaving only one table seeded.
    with transaction(), _connection() as conn, conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO group_budgets (group_name, weekly_token_budget) "
            "VALUES (%s, %s) ON CONFLICT (group_name) DO NOTHING",
            _GROUP_BUDGET_SEED,
        )
        cursor.executemany(
            "INSERT INTO app_config (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
            [(key, jsonb(value)) for key, value in _APP_CONFIG_SEED],
        )


def get_config(key: str, default: Any = None) -> Any:
    """A seeded key's declared value IS its default, so an unseeded row behaves
    exactly like a seeded one.

    The seed runs after `_migrate()`, in its own transaction and outside the
    advisory lock. A container that migrates and then dies before reaching it
    leaves alembic reporting head with the row absent - and every caller that
    passed its own fallback silently getting that fallback instead. For a
    feature gate that reads as "the feature did not ship", with no error, no
    exception and nothing for a health check to see.

    Falling back to the seed is not failing open: it is the same value a
    successful seed would have written. `default` still covers keys that are
    not seeded at all.
    """
    row = query_one("SELECT value FROM app_config WHERE key = %s", (key,))
    if row:
        return row["value"]
    return _APP_CONFIG_DEFAULTS.get(key, default)


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    from core.paths import PROJECT_ROOT

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")


def _as_query(sql: str) -> LiteralString:
    """psycopg types execute() to accept only LiteralString, which is a good
    default: it stops a caller passing a runtime-built string. This codebase
    does build SQL at runtime, but only by interpolating whitelisted fragments
    (_SORTABLE, fixed column names, re.escape'd literals) - never a user value,
    which always travels as a bound parameter.

    The cast states that invariant in one place instead of scattering a type
    ignore over every call site, so if the invariant ever breaks there is a
    single obvious thing to re-read.
    """
    return cast("LiteralString", sql)


def query(sql: str, params: Any = None) -> list[dict[str, Any]]:
    with _connection() as conn:
        return [dict(r) for r in conn.execute(_as_query(sql), params).fetchall()]


def query_one(sql: str, params: Any = None) -> dict[str, Any] | None:
    with _connection() as conn:
        row = conn.execute(_as_query(sql), params).fetchone()
    return dict(row) if row else None


def query_as[Row](row: type[Row], sql: str, params: Any = None) -> list[Row]:
    """query(), with each row built into a shape a type checker knows.

    The SQL stays exactly as written. This is not an ORM and must not become
    one: the hot paths here carry query plans measured against production
    (api/board/visibility.py records 320ms falling to 28ms, and why), and
    hiding them behind a query builder would hide the one thing that has to
    stay readable. What a dict costs is different and smaller: a renamed
    column is a grep across every call site, and a typo is a KeyError at
    runtime rather than a red line in the editor.

    A column the shape does not declare raises TypeError here rather than
    being carried silently, which is the point: a SELECT and its shape drift
    apart in one commit and are found in the next test run, not in a bug
    report about a missing field.
    """
    with _connection() as conn:
        return [row(**r) for r in conn.execute(_as_query(sql), params).fetchall()]


def query_one_as[Row](row: type[Row], sql: str, params: Any = None) -> Row | None:
    with _connection() as conn:
        got = conn.execute(_as_query(sql), params).fetchone()
    return row(**got) if got else None


def execute(sql: str, params: Any = None) -> None:
    with _connection() as conn:
        conn.execute(_as_query(sql), params)


def execute_count(sql: str, params: Any = None) -> int:
    """execute(), but returns how many rows it touched - for the callers whose
    whole purpose is that number (the reaper counting requeues, say)."""
    with _connection() as conn:
        return conn.execute(_as_query(sql), params).rowcount


def _json_default(value: Any) -> Any:
    """psycopg's default dumps refuses the types SQL hands back. A detector
    building its detail from dict(row) gets Decimal for every rate and
    datetime for every timestamp, and the write fails at the driver with a
    message naming neither the column nor the detector."""
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return str(value)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=_json_default))


def executemany(sql: str, params: Any) -> None:
    with _connection() as conn:
        conn.cursor().executemany(_as_query(sql), params)
