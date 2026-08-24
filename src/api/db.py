from __future__ import annotations

import atexit
import os
from typing import Any, Dict, List, Optional

import dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

dotenv.load_dotenv()

pool = ConnectionPool(
    os.environ["DATABASE_URL"],
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=True,
)
atexit.register(pool.close)

# Weekly owner-key token budgets by Authentik group. Seeded once with ON
# CONFLICT DO NOTHING so runtime edits via /v1/admin/group-budgets stick;
# groups absent from the table (e.g. jobtracker-users-public) are BYO-only.
_GROUP_BUDGET_SEED = [
    ("infra-admins", None),
    ("jobtracker-users-internal", 5_000_000),
]


_APP_CONFIG_SEED = [("signups_enabled", True)]


def init_schema() -> None:
    _migrate()
    _seed_sources()
    for group, tokens in _GROUP_BUDGET_SEED:
        execute(
            "INSERT INTO group_budgets (group_name, weekly_token_budget) "
            "VALUES (%s, %s) ON CONFLICT (group_name) DO NOTHING",
            (group, tokens),
        )
    for key, value in _APP_CONFIG_SEED:
        execute(
            "INSERT INTO app_config (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO NOTHING",
            (key, jsonb(value)),
        )


def get_config(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM app_config WHERE key = %s", (key,))
    return row["value"] if row else default


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config

    from core.paths import PROJECT_ROOT

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")


def _seed_sources() -> None:
    from core.configs import load_configs

    for name, cfg in load_configs().items():
        execute(
            """
            INSERT INTO sources (name, listings_url) VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET listings_url = EXCLUDED.listings_url
            """,
            (name, cfg["JOB_LISTINGS_URL"]),
        )


def query(sql: str, params: Any = None) -> List[Dict[str, Any]]:
    with pool.connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: Any = None) -> Optional[Dict[str, Any]]:
    with pool.connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def execute(sql: str, params: Any = None) -> None:
    with pool.connection() as conn:
        conn.execute(sql, params)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)
