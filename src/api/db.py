from __future__ import annotations

import datetime
import decimal
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, LiteralString, cast

from psycopg.types.json import Jsonb

from api.admin_groups import ADMIN_GROUPS
from api.config import CONFIG_KEYS

# The pool and its instrumentation live in core.pool, shared by store and
# catalog writes.
from core.pool import connection as _connection
from core.pool import pool, statement, transaction

# Weekly owner-key token budgets by Authentik group. Seeded once with ON
# CONFLICT DO NOTHING so runtime edits via /v1/admin/group-budgets stick;
# groups absent from the table (e.g. jobtracker-users-public) are BYO-only.
# The admin groups (JOBTRACKER_ADMIN_GROUPS) have no cap.
_GROUP_BUDGET_SEED = [
    *((group, None) for group in sorted(ADMIN_GROUPS)),
    ("jobtracker-users-internal", 5_000_000),
]


_APP_CONFIG_SEED = [(key, spec.default) for key, spec in CONFIG_KEYS.items()]
_APP_CONFIG_DEFAULTS = dict(_APP_CONFIG_SEED)


# Advisory lock key serializing startup `alembic upgrade head` across hosts: on a
# lockstep roll up to four processes can enter it within the same minute.
_SCHEMA_LOCK_KEY = 8_274_113_907_441_002
# 1 s of polling slack is invisible beside a peer's seconds-long migration.
_SCHEMA_LOCK_POLL_SECONDS = 1.0


def init_schema() -> None:
    # Wait, never skip: running against a half-migrated schema is wrong.
    # Poll pg_try_advisory_lock and commit every attempt so neither waiters nor
    # the lock holder keep an open snapshot that blocks CREATE INDEX CONCURRENTLY
    # (reproduced 2026-10-03). The session advisory lock outlives the commit.
    with pool.connection() as conn:
        try_lock = "SELECT pg_try_advisory_lock(%s) AS held"
        while True:
            held = conn.execute(try_lock, (_SCHEMA_LOCK_KEY,)).fetchone() == {"held": True}
            conn.commit()
            if held:
                break
            time.sleep(_SCHEMA_LOCK_POLL_SECONDS)
        try:
            _migrate()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_KEY,))
    seed_defaults()


def seed_defaults() -> None:
    # Seed both tables in one transaction while preserving existing overrides.
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
    """Read an app_config key, falling back to its seeded default if unseeded.

    The seed runs after `_migrate()`, so a container that migrates and then dies
    leaves the row absent. A caller's own fallback would then silently turn a
    feature gate off; the seeded value is what a successful seed would have written.
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
    """Centralize the LiteralString cast for queries built from whitelisted SQL fragments.

    Runtime SQL only interpolates whitelisted fragments; a user value always travels
    as a bound parameter.
    """
    return cast("LiteralString", sql)


def query(sql: str, params: Any = None) -> list[dict[str, Any]]:
    with statement() as conn:
        return [dict(r) for r in conn.execute(_as_query(sql), params).fetchall()]


def query_one(sql: str, params: Any = None) -> dict[str, Any] | None:
    with statement() as conn:
        row = conn.execute(_as_query(sql), params).fetchone()
    return dict(row) if row else None


def query_as[Row](row: type[Row], sql: str, params: Any = None) -> list[Row]:
    """Run raw SQL and construct `row(**r)` per result row (raises TypeError on column drift).

    Not an ORM and must not become one: hot-path SQL carries query plans measured
    against production, and a query builder would hide them.
    """
    with statement() as conn:
        return [row(**r) for r in conn.execute(_as_query(sql), params).fetchall()]


def query_one_as[Row](row: type[Row], sql: str, params: Any = None) -> Row | None:
    with statement() as conn:
        got = conn.execute(_as_query(sql), params).fetchone()
    return row(**got) if got else None


def execute(sql: str, params: Any = None) -> None:
    with statement() as conn:
        conn.execute(_as_query(sql), params)


@contextmanager
def pipeline() -> Iterator[None]:
    """Batch write-only statements in a psycopg pipeline; replies are read on exit."""
    with _connection() as conn, conn.pipeline():
        yield


def execute_count(sql: str, params: Any = None) -> int:
    """Execute a statement and return its affected rowcount."""
    with statement() as conn:
        return conn.execute(_as_query(sql), params).rowcount


def _json_default(value: Any) -> Any:
    """Coerce SQL result types (Decimal, date/datetime) that json.dumps rejects."""
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
