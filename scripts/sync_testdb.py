"""Refresh the test database from production, on demand.

The copy runs SERVER-SIDE, via postgres_fdw. Both databases live on the same
instance, so routing rows through the machine running this script would send
every byte across the WAN twice - measured at 218 ms round-trip, with 450 MB
in ai_queries alone. postgres_fdw keeps the data on the server and this script
only issues the statements.

Deliberately not pg_dump: the local client has to match the server major
version and often does not, and requiring Docker to paper over that puts a
daemon between you and running tests.

The schema is built by the application's own migrations rather than copied, so
a sync also proves that migrations produce the schema production actually has.

Usage:
    set -a && . ./.env && set +a
    python scripts/sync_testdb.py            # structure + data
    python scripts/sync_testdb.py --fast     # skips ai_queries.input_content
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from urllib.parse import urlparse, urlunparse

import psycopg

# Copy order is FK order. Foreign keys are disabled during the copy, but a
# deterministic order keeps a partial failure readable.
TABLES = [
    "users",
    "sources",
    "source_groups",
    "group_budgets",
    "app_config",
    "filter_presets",
    "jobs",
    "user_settings",
    "user_sources",
    "user_filters",
    "user_jobs",
    "user_job_history",
    "ai_queries",
    "ai_batches",
    "api_usage",
    "tasks",
    "worker_status",
    "reports",
    "source_requests",
    "health_alerts",
]

# input_content is ~80% of the database and almost no test needs page text.
FAST_SKIP = {"ai_queries": ["input_content", "instructions", "parsed_json"]}


def _swap_db(url: str, name: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{name}"))


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND table_schema = 'public' ORDER BY ordinal_position",
            (table,),
        ).fetchall()
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="jobtracker_test")
    ap.add_argument("--fast", action="store_true", help="skip the large text columns")
    ap.add_argument(
        "--reader-role",
        metavar="NAME",
        help="create/rotate a SELECT-only login on the test db and print its URL, "
        "so CI never holds the production superuser",
    )
    args = ap.parse_args()

    src_url = os.environ.get("DATABASE_URL")
    if not src_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    if not (args.name.endswith(("_test", "_ci")) or args.name.startswith("test_")):
        print(f"refusing to target {args.name!r}: name it *_test or *_ci", file=sys.stderr)
        return 1

    dst_url = _swap_db(src_url, args.name)
    admin_url = _swap_db(src_url, "postgres")

    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{args.name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{args.name}"')
    print(f"created {args.name}")

    # Build the schema the same way production got it. If a migration is broken
    # this fails here, which is a genuinely useful thing for a sync to catch.
    env = {**os.environ, "DATABASE_URL": dst_url, "PYTHONPATH": "src"}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True)
    subprocess.run(
        [sys.executable, "-c", "import core.store"], env=env, check=True
    )  # creates ai_queries, which alembic does not own
    print("schema built from migrations")

    total = 0
    src_db = urlparse(src_url).path.lstrip("/")
    with psycopg.connect(dst_url) as dst:
        # postgres_fdw makes the source database readable from inside the
        # target, so INSERT ... SELECT never leaves the server. The password is
        # required in the user mapping; it is the same credential this script
        # already holds.
        parts = urlparse(src_url)
        dst.execute("CREATE EXTENSION IF NOT EXISTS postgres_fdw")
        dst.execute(
            "CREATE SERVER src_srv FOREIGN DATA WRAPPER postgres_fdw "
            "OPTIONS (host %s, port %s, dbname %s)".replace("%s", "{}").format(
                f"'{parts.hostname}'", f"'{parts.port or 5432}'", f"'{src_db}'"
            )
        )
        dst.execute(
            "CREATE USER MAPPING FOR CURRENT_USER SERVER src_srv "
            f"OPTIONS (user '{parts.username}', password '{parts.password}')"
        )
        dst.execute("CREATE SCHEMA src_remote")
        dst.commit()
        # Pull the source tables in as foreign tables, then INSERT..SELECT.
        # Postgres performs the read and the write inside the server; nothing
        # travels to this machine.
        dst.execute(
            "IMPORT FOREIGN SCHEMA public LIMIT TO ({}) FROM SERVER src_srv INTO src_remote".format(
                ", ".join(TABLES)
            )
        )
        dst.commit()

        # FK order is not enough on its own - rows can reference others in the
        # same table. Disabling the triggers makes the copy order-independent.
        dst.execute("SET session_replication_role = replica")
        for table in TABLES:
            src_cols = {
                r[0]
                for r in dst.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'src_remote' AND table_name = %s",
                    (table,),
                ).fetchall()
            }
            dst_cols = _columns(dst, table)
            if not dst_cols or not src_cols:
                print(f"  {table}: not present on both sides, skipped")
                continue
            skip = set(FAST_SKIP.get(table, []) if args.fast else [])
            cols = [c for c in dst_cols if c in src_cols and c not in skip]
            collist = ", ".join(f'"{c}"' for c in cols)
            # OVERRIDING SYSTEM VALUE because the id columns are GENERATED
            # ALWAYS: a copy has to preserve the production ids, or every
            # foreign key in the copied data points at the wrong row.
            dst.execute(
                f"INSERT INTO public.{table} ({collist}) OVERRIDING SYSTEM VALUE "
                f"SELECT {collist} FROM src_remote.{table}"
            )
            n = dst.execute(f"SELECT count(*) FROM public.{table}").fetchone()
            count = n[0] if n else 0
            total += count
            print(
                f"  {table}: {count} rows"
                + (f" (skipped {', '.join(sorted(skip))})" if skip else "")
            )

        # Sequences do not follow the rows; without this the first insert in a
        # test collides with a synced row.
        for row in dst.execute(
            """
            SELECT c.relname AS seq, t.relname AS tbl, a.attname AS col
            FROM pg_class c
            JOIN pg_depend d ON d.objid = c.oid AND d.deptype = 'a'
            JOIN pg_class t ON t.oid = d.refobjid
            JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
            WHERE c.relkind = 'S' AND t.relnamespace = 'public'::regnamespace
            """
        ).fetchall():
            seq, tbl, col = row
            dst.execute(
                f'SELECT setval(%s, COALESCE((SELECT MAX("{col}") FROM public."{tbl}"), 1))',
                (seq,),
            )
        dst.execute("SET session_replication_role = DEFAULT")

        # Tear the link down. The user mapping stores the production password,
        # and leaving it behind would put a live credential inside a database
        # whose whole purpose is being disposable.
        dst.execute("DROP SCHEMA src_remote CASCADE")
        dst.execute("DROP USER MAPPING IF EXISTS FOR CURRENT_USER SERVER src_srv")
        dst.execute("DROP SERVER IF EXISTS src_srv CASCADE")
        dst.execute("DROP EXTENSION IF EXISTS postgres_fdw CASCADE")
        dst.commit()

    if args.reader_role:
        _grant_reader(dst_url, args.reader_role, args.name)

    print(f"\nsynced {total} rows into {args.name}")
    print("run integration tests with:")
    print(f"  TEST_DATABASE_URL='{dst_url}' make integration")
    return 0


def _grant_reader(dst_url: str, role: str, dbname: str) -> None:
    """Create/refresh a login role with SELECT on the test database only.

    CI should never hold the production superuser. This role can read the
    synced copy and nothing else - not production, and not write access to the
    copy - which is all the integration tests need.
    """
    import secrets

    from psycopg import sql

    password = secrets.token_urlsafe(24)
    role_id = sql.Identifier(role)
    # A role password cannot be a bound parameter, so it has to be inlined.
    # sql.Literal does the quoting rather than an f-string doing it by hand.
    pw = sql.Literal(password)
    with psycopg.connect(dst_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = sql.SQL("ALTER ROLE") if exists else sql.SQL("CREATE ROLE")
        conn.execute(sql.SQL("{} {} WITH LOGIN PASSWORD {}").format(verb, role_id, pw))
        conn.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO "{role}"')
        conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        conn.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"')
        conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO "{role}"'
        )
    parts = urlparse(dst_url)
    reader_url = urlunparse(
        parts._replace(netloc=f"{role}:{password}@{parts.hostname}:{parts.port or 5432}")
    )
    print(f"\nread-only role {role!r} refreshed. Set this as the CI secret:")
    print(f"  gh secret set TEST_DATABASE_URL --body '{reader_url}'")


if __name__ == "__main__":
    raise SystemExit(main())
