"""Reset mutable test tables after schema provisioning."""

from __future__ import annotations

from api import db

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


def _names(tables: list[str]) -> str:
    return ", ".join(f'"{t}"' for t in tables)


# Read once: the migrations ran above, and no test adds a table.
_TABLES = _mutable_tables()

# Emptying a table costs about the same whether it held one row or none -
# roughly 5 ms per relation for TRUNCATE, which over 51 tables and 1,650 tests
# was the largest single cost in the suite. Measured 2026-09-11 on one
# checkout's database: 360 ms per reset, against CI setup phases of 133-145 s
# in a 171-186 s serial run. A test dirties a handful of tables, so the reset
# asks which ones rather than assuming all of them.
#
# DELETE rather than TRUNCATE for the ordinary case: TRUNCATE rewrites a
# relation file and takes an ACCESS EXCLUSIVE lock per table, while deleting
# the dozen rows a test wrote is nearly free. Measured on the same database
# with the same test-shaped state: truncate all 51 = 360 ms, truncate only the
# dirty ones = 159 ms (a test that touches `users` drags its 28-table foreign
# key closure in through CASCADE), delete the dirty ones = 10.7 ms.
#
# Above this many rows a table goes back to TRUNCATE, where row-by-row
# deletion stops being the cheap option - the corpus tables, which hold tens
# of thousands.
_BULK_ROWS = 5_000

# One round trip: which tables have rows, and how many, counting no further
# than the threshold that changes the decision.
_DIRTY_TABLES = " UNION ALL ".join(
    f"SELECT '{t}' AS t, "
    f'(SELECT count(*) FROM (SELECT 1 FROM "{t}" LIMIT {_BULK_ROWS + 1}) s) AS n '
    f'WHERE EXISTS (SELECT 1 FROM "{t}")'
    for t in _TABLES
)

# RESTART IDENTITY only resets the sequences of the tables it truncates, and a
# test that inserted and then deleted leaves an advanced sequence behind an
# empty table. Restarting those keeps every id starting from 1, as it did when
# every table was truncated every time.
_USED_SEQUENCES = (
    "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public' AND last_value IS NOT NULL"
)

# Deleting in an order no foreign key objects to would mean sorting 51 tables
# by their dependencies on every reset. Turning the constraints off for the
# statement is one line and needs superuser, which every test database here
# has; where it does not, the reset truncates the dirty set instead.
_CAN_SKIP_CONSTRAINTS = bool(
    db.query_one("SELECT current_setting('is_superuser') = 'on' AS ok")["ok"]
)


def _reset_to_empty() -> None:
    """Leave the database as a truncate-everything reset left it: no rows
    outside the persistent tables, every sequence back at its start, and the
    seeded defaults present."""
    rows = db.query(_DIRTY_TABLES)
    if rows:
        bulk = [r["t"] for r in rows if r["n"] > _BULK_ROWS]
        small = [r["t"] for r in rows if r["n"] <= _BULK_ROWS]
        if bulk:
            db.execute(f"TRUNCATE TABLE {_names(bulk)} RESTART IDENTITY CASCADE")
        if small and _CAN_SKIP_CONSTRAINTS:
            # One statement, so one transaction: a failure rolls the session
            # setting back with the deletes, and a pooled connection never
            # escapes this function with its constraints still disabled.
            db.execute(
                "SET session_replication_role = replica; "
                + "; ".join(f'DELETE FROM "{t}"' for t in small)
                + "; SET session_replication_role = origin"
            )
        elif small:
            db.execute(f"TRUNCATE TABLE {_names(small)} RESTART IDENTITY CASCADE")
    advanced = [r["sequencename"] for r in db.query(_USED_SEQUENCES)]
    if advanced:
        db.execute("; ".join(f'ALTER SEQUENCE "{s}" RESTART' for s in advanced))
    _reseed()


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
    db.seed_defaults()
