"""Joining a bundle no longer copies its members, and e71b4d2c9a08 removes
the copies already made without changing anyone's set of sources."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from api import db, source_selection

_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "e71b4d2c9a08_drop_picks_a_followed_bundle_reaches.py"
)


def _upgrade() -> None:
    spec = importlib.util.spec_from_file_location("follow_contract", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine(
        os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://")
    )
    try:
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
    finally:
        engine.dispose()


def _picks(uid: int) -> list[str]:
    return [
        r["source"]
        for r in db.query(
            "SELECT source FROM user_sources WHERE user_id = %s ORDER BY source", (uid,)
        )
    ]


def test_join_does_not_copy_and_old_copies_go(f):
    uid = f.make_user()
    for name in ("a", "b", "solo"):
        f.make_source(name)
    source_selection.save_group("g", ["a", "b"], "", None)
    source_selection.join_group(uid, "g", only=False)
    assert _picks(uid) == []

    # Copies an older server made, beside a pick no bundle reaches.
    db.execute(
        "INSERT INTO user_sources (user_id, source) VALUES (%s, 'a'), (%s, 'b'), (%s, 'solo')",
        (uid, uid, uid),
    )
    before = source_selection.held(uid)
    _upgrade()
    _upgrade()
    assert _picks(uid) == ["solo"]
    assert source_selection.held(uid) == before == ["a", "b", "solo"]
