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

WHAT THIS IS FOR, NOW THAT THERE IS A CORPUS. Most tests no longer want this.
`tests/corpus.py` generates a catalog from a committed measurement of
production, runs on every pull request, and holds several users - which this
never will, because production has one. What is left here is the small set of
checks whose subject is what a live writer actually did, and a dev API over
real rows. See docs/agents/testing.md.

Every identifying column is rewritten on the way out (see ANONYMISE). The
mailbox, the addresses and the OAuth tokens do not travel.

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

# Which tables get copied is read from the SOURCE at run time, not listed
# here. A constant WAS listed here, and it had drifted eleven populated tables
# behind production - job_skills, email_events, email_messages,
# job_embeddings, application_matches, job_requirements, applications,
# action_items, ai_prompt_samples, ai_prompts, user_oauth_tokens, 345,032 rows
# between them - and nothing said so. The "not present on both sides" message
# below only ever fired for a table that WAS in the list, so a table missing
# from the list produced no output at all and the sync printed a total and
# looked like it had worked.
#
# Deriving it means a new table is copied the day it exists, and a table the
# local schema does not have is reported instead of skipped in silence.
EXCLUDE_TABLES = {
    # Written by the `alembic upgrade head` below. Copying it would put
    # production's revision row beside the one the migrations just wrote.
    "alembic_version",
}

# Columns rewritten during the copy, never selected verbatim.
#
# The copy lands on developer laptops and used to land in a GitHub Actions
# secret. It carried the user's email address, their entire imported mailbox -
# every sender, subject and body - and their Google refresh token, encrypted
# under a key those same machines hold. None of that is needed to check a
# query plan or a column's shape, and there is no version of "we will remember
# to be careful with it" that survives a year.
#
# The expressions run inside the INSERT ... SELECT, so the original value
# never leaves the server it was already on.
ANONYMISE = {
    "users.email": "'user' || id || '@example.invalid'",
    "users.name": "'User ' || id",
    "users.sub": "'copied-sub-' || id",
    "user_settings.api_key_enc": "NULL",
    "user_settings.digest_token": "NULL",
    "user_settings.identities": "NULL",
    # NOT NULL, so it cannot simply be dropped - and a random blob would fail
    # to decrypt somewhere far from here. Marked invalid instead, which is a
    # state the application already knows how to handle: a disconnected
    # mailbox that asks its owner to reconnect.
    "user_oauth_tokens.refresh_token_enc": r"'\x00'::bytea",
    "user_oauth_tokens.access_token_enc": "NULL",
    "user_oauth_tokens.account_email": "'user' || user_id || '@example.invalid'",
    "user_oauth_tokens.invalid_at": "now()",
    "user_oauth_tokens.invalid_reason": "'anonymised in the test copy'",
    "email_messages.from_email": "'sender' || id || '@example.invalid'",
    "email_messages.from_name": "'Sender ' || id",
    "email_messages.to_emails": "ARRAY['user' || user_id || '@example.invalid']",
    "email_messages.subject": "'subject ' || id",
    "email_messages.thread_topic": "NULL",
    # Length preserved, content not. The prefilter and the classifier are
    # sensitive to how long a body is; nothing downstream needs the words.
    "email_messages.body_text": "repeat('x', length(body_text))",
    "email_messages.body_html": "repeat('x', length(body_html))",
    "email_messages.headers": "NULL",
    "user_jobs.notes": "NULL",
    "user_jobs.recruiter": "NULL",
    "user_jobs.connection1": "NULL",
    "user_jobs.connection2": "NULL",
    "user_jobs.documents": "NULL",
    "reports.message": "'report ' || id",
    "reports.resolution_note": "NULL",
    "source_requests.note": "'request ' || id",
    "source_requests.resolution_note": "NULL",
    "application_matches.rationale": "NULL",
}

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
    ap.add_argument(
        "--dev-role",
        metavar="NAME",
        help="create/rotate a READ-WRITE login on the test db and print its URL, "
        "for a local dev API. Writes, because the app provisions a user on the "
        "first authenticated request and the whole point is to click around",
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
        # Pull EVERY source table in as a foreign table, then INSERT..SELECT.
        # Postgres performs the read and the write inside the server; nothing
        # travels to this machine. No LIMIT TO: what exists in production is
        # what gets copied, so the list cannot fall behind it.
        dst.execute("IMPORT FOREIGN SCHEMA public FROM SERVER src_srv INTO src_remote")
        dst.commit()

        source_tables = {
            r[0]
            for r in dst.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'src_remote'"
            ).fetchall()
        }
        local_tables = {
            r[0]
            for r in dst.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        missing = sorted(source_tables - local_tables - EXCLUDE_TABLES)
        if missing:
            # Loud, and fatal. A table in production that the migrations do not
            # produce means the copy would be silently partial, which is the
            # exact failure this script used to have.
            print(
                f"\nERROR: production has {len(missing)} table(s) the migrations "
                f"did not create: {', '.join(missing)}\n"
                "The copy would be silently incomplete. Add the migration.",
                file=sys.stderr,
            )
            return 1
        tables = sorted((source_tables & local_tables) - EXCLUDE_TABLES)
        print(f"copying {len(tables)} tables")

        # FK order is not enough on its own - rows can reference others in the
        # same table. Disabling the triggers makes the copy order-independent.
        dst.execute("SET session_replication_role = replica")
        for table in tables:
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
            redacted = [c for c in cols if f"{table}.{c}" in ANONYMISE]
            selects = ", ".join(
                ANONYMISE.get(f"{table}.{c}", f'"{c}"') + f' AS "{c}"' for c in cols
            )
            # OVERRIDING SYSTEM VALUE because the id columns are GENERATED
            # ALWAYS: a copy has to preserve the production ids, or every
            # foreign key in the copied data points at the wrong row.
            dst.execute(
                f"INSERT INTO public.{table} ({collist}) OVERRIDING SYSTEM VALUE "
                f"SELECT {selects} FROM src_remote.{table}"
            )
            n = dst.execute(f"SELECT count(*) FROM public.{table}").fetchone()
            count = n[0] if n else 0
            total += count
            print(
                f"  {table}: {count} rows"
                + (f" (skipped {', '.join(sorted(skip))})" if skip else "")
                + (f" (anonymised {', '.join(redacted)})" if redacted else "")
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

    _stamp_synced_at(dst_url)

    if args.reader_role:
        _grant_reader(dst_url, args.reader_role, args.name)
    if args.dev_role:
        _grant_dev(dst_url, args.dev_role, args.name)

    print(f"\nsynced {total} rows into {args.name}")
    print("run integration tests with:")
    print(f"  TEST_DATABASE_URL='{dst_url}' make integration")
    return 0


def _stamp_synced_at(dst_url: str) -> None:
    """Record when this copy was cut, IN the copy.

    A copy that goes stale without anyone noticing is the fixture problem one
    layer out: the data is real, it is just no longer true. Anyone reading the
    copy - a dev API, a frontend rendering against it, an integration run -
    can now say how old the thing they are looking at is, without needing
    access to whatever cut it.

    In app_config rather than a file because a file does not travel with a
    database, and the question is always asked of the data.
    """
    import datetime
    import json

    with psycopg.connect(dst_url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO app_config (key, value) VALUES ('testdb_synced_at', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (json.dumps(datetime.datetime.now(tz=datetime.UTC).isoformat()),),
        )


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


def _grant_dev(dst_url: str, role: str, dbname: str) -> None:
    """Create/refresh a login with read-write on the COPY and nothing else.

    THE ISOLATION HERE IS A CREDENTIAL, NOT A NETWORK. jobtracker-db is
    deliberately published on the public internet - that is how the oci and
    kanishk-desktop workers reach it - so "it runs locally" buys nothing at
    all: a process handed the production DSN connects from anywhere on earth.
    What keeps a dev API off production is that this role cannot log into it.

    Read-write rather than SELECT-only because the app writes on the first
    authenticated request: require_user provisions a user row, and a dev API
    that 500s on sign-in is not a dev API. That is also why this role must
    never be pointed at production - it can write whatever it reaches.
    """
    import secrets

    from psycopg import sql

    password = secrets.token_urlsafe(24)
    role_id = sql.Identifier(role)
    pw = sql.Literal(password)
    with psycopg.connect(dst_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = sql.SQL("ALTER ROLE") if exists else sql.SQL("CREATE ROLE")
        conn.execute(sql.SQL("{} {} WITH LOGIN PASSWORD {}").format(verb, role_id, pw))
        # NOSUPERUSER/NOCREATEDB stated rather than assumed: an ALTER on a role
        # that already exists inherits whatever it had, so a role that was
        # once something else does not quietly stay that way.
        conn.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(role_id))
        conn.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO "{role}"')
        conn.execute(f'GRANT USAGE, CREATE ON SCHEMA public TO "{role}"')
        conn.execute(f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "{role}"')
        conn.execute(f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{role}"')
        conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON TABLES TO "{role}"'
        )
        conn.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f'GRANT ALL PRIVILEGES ON SEQUENCES TO "{role}"'
        )
    parts = urlparse(dst_url)
    dev_url = urlunparse(
        parts._replace(netloc=f"{role}:{password}@{parts.hostname}:{parts.port or 5432}")
    )
    print(f"\ndev role {role!r} refreshed. It can read and write {dbname} and NOTHING else.")
    print(f"  export JOBTRACKER_DEV_DATABASE_URL='{dev_url}'")
    print("  make dev-api")


if __name__ == "__main__":
    raise SystemExit(main())
