"""Build synthetic rows from measured production shapes.

`tests/production_profile.json` supplies value distributions, null rates,
string lengths and timestamp ranges. Schema foreign keys and LINKS in
corpus_generator/config.py supply relationships. The corpus contains no
production rows and supports multiple users in pull request tests.

Do not encode writer invariants into generated rows. Tests of those
invariants need a synced production copy; otherwise the fixture would make
their assertions true by construction. The `corpus` marker invokes build().
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from api import db
from api.mail import current as mail_current
from core.disposable_db import require_disposable_dsn
from tests.corpus_generator.config import (
    LINKS,
    PROFILE_PATH,
    SEED,
    SEEDED,
    SKIP,
    TARGET_JOBS,
    USERS,
)
from tests.corpus_generator.relationships import _specialise
from tests.corpus_generator.rows import _Generator
from tests.corpus_generator.schema import _order, _Schema


def _insert(table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    names = list(rows[0])
    collist = ", ".join(f'"{c}"' for c in names)
    placeholders = ", ".join(f"%({c})s" for c in names)
    sql = f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    with db.pool.connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)


def _parent_columns(schema: _Schema) -> dict[str, set[str]]:
    wanted: dict[str, set[str]] = {}
    for parent, column in list(schema.fks.values()) + list(LINKS.values()):
        wanted.setdefault(parent, set()).add(column)
    return wanted


def build(*, target_jobs: int = TARGET_JOBS) -> dict[str, int]:
    """Fill the current database with a corpus. Returns rows written per table.

    Destructive: everything is truncated first. The caller is the test suite,
    against a database whose name conftest has already refused to accept
    unless it is disposable.
    """
    _refuse_to_destroy_anything_that_matters()
    profile = json.loads(PROFILE_PATH.read_text())
    schema = _Schema()

    tables = [
        t for t in profile["tables"] if t not in SKIP and t in schema.columns and t not in SEEDED
    ]
    scale = target_jobs / max(1, profile["tables"]["jobs"]["rows"])
    gen = _Generator(profile, schema, scale)
    wanted = _parent_columns(schema)

    everything = [t for t in schema.columns if t not in SKIP]
    db.execute(f"TRUNCATE TABLE {', '.join(everything)} RESTART IDENTITY CASCADE")
    db.init_schema()  # re-seeds sources, source_groups, app_config, group_budgets

    for table in SEEDED:
        for column in wanted.get(table, set()):
            gen.load_pool(table, column)

    written: dict[str, int] = {}
    for table in _order(schema, tables):
        n = USERS if table == "users" else gen._row_count(table)
        rows = gen.rows(table, n)
        _specialise(table, rows, gen)
        _insert(table, rows)
        for column in wanted.get(table, set()):
            gen.load_pool(table, column)
        count = db.query_one(f"SELECT count(*) AS c FROM {table}")
        written[table] = int(count["c"]) if count else 0

    _materialise_boards()
    # The mail logs were drawn row by row, not appended through their writers,
    # so each message's current pointers are set here to what the writers
    # would have left: the newest row of each log. Assigned, not GREATEST, in
    # case the generator drew a value for the columns themselves.
    db.execute(
        f"""
        UPDATE email_messages m SET current_event_id = n.event_id, current_match_id = n.match_id
        FROM ({mail_current.NEWEST_IDS}) n WHERE m.id = n.message_id
        """
    )
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('testdb_corpus_seed', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (
            db.jsonb(
                {
                    "seed": SEED,
                    "profile_measured_at": profile["measured_at"],
                    "fingerprint": fingerprint(),
                }
            ),
        ),
    )
    return written


def _refuse_to_destroy_anything_that_matters() -> None:
    """Reject a non-disposable database or a synced production copy.

    `make corpus` runs outside conftest's database-name guard. Building over a
    synced copy would erase integration evidence and could make tests skip.
    Check both conditions before truncation.
    """
    name = require_disposable_dsn(os.environ["DATABASE_URL"])
    if holds_real_data():
        raise RuntimeError(
            f"{name!r} holds a synced copy of production, and building the corpus "
            "would truncate it.\n"
            "Point the suite at a different database, or select only the tests "
            "that want the copy:\n"
            "    make integration"
        )


def _materialise_boards() -> None:
    """Run the application's OWN write-time board predicate over the corpus.

    Deliberately not a hand-written INSERT. The board is defined twice - as a
    read-time predicate in routers/jobs.py and a write-time one in
    tasks/board.py - and `test_the_two_visibility_predicates_agree` exists
    because they have already drifted. If the corpus materialised boards its
    own way, that test would compare the read path against the corpus instead
    of against the write path, and stop being able to see the drift at all.
    """
    from tasks.board import materialize_passing

    for row in db.query("SELECT id FROM users ORDER BY id"):
        materialize_passing(row["id"])


def fingerprint() -> str:
    """This generator plus the profile it reads, hashed.

    Presence alone is not enough to reuse a corpus. Editing the generator or
    re-measuring the profile leaves a database that still LOOKS built, so the
    suite reuses it and the change appears to have had no effect - which is a
    test that silently stopped being able to fail. Reproduced while checking
    exactly that: four deliberate breakages of the generator all still passed,
    against a corpus built before them.
    """
    generator_files = (
        Path(__file__),
        *sorted((Path(__file__).parent / "corpus_generator").glob("*.py")),
    )
    digest = hashlib.sha256()
    for path in (*generator_files, PROFILE_PATH):
        digest.update(path.relative_to(Path(__file__).parent).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def is_present() -> bool:
    row = db.query_one("SELECT value FROM app_config WHERE key = 'testdb_corpus_seed'")
    return bool(row) and row["value"].get("fingerprint") == fingerprint()


def holds_real_data() -> bool:
    """True when this database is a synced copy of production rather than a
    corpus. `sync_testdb.py` stamps when the copy was cut; `build()` stamps
    that it generated one. A database with neither is an empty scratch."""
    row = db.query_one("SELECT 1 AS t FROM app_config WHERE key = 'testdb_synced_at'")
    return bool(row) and not is_present()
