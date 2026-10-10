"""c3e8a1f05d72 follows exactly the bundles a person holds whole, and leaves
every person's set of sources as it was."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from api import db, source_selection

_PATH = (
    Path(__file__).parents[1] / "alembic" / "versions" / "c3e8a1f05d72_follow_fully_held_bundles.py"
)


def _upgrade() -> None:
    spec = importlib.util.spec_from_file_location("follow_backfill", _PATH)
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


def test_follows_bundles_held_whole_only(f):
    uid, other = f.make_user(), f.make_user()
    for name, active in (("a", True), ("b", True), ("c", True), ("off", False)):
        f.make_source(name, active=active)
    # Held whole (the switched-off member is not on offer), held in part,
    # and held by nobody.
    source_selection.save_group("whole", ["a", "off"], "", None)
    source_selection.save_group("part", ["a", "b"], "", None)
    source_selection.save_group("none", ["c"], "", None)
    source_selection.replace(uid, ["a"])
    source_selection.replace(other, ["b", "c"])
    before = (source_selection.held(uid), source_selection.held(other))

    _upgrade()
    _upgrade()

    assert source_selection.followed(uid) == {"whole"}
    assert source_selection.followed(other) == {"none"}
    assert (source_selection.held(uid), source_selection.held(other)) == before
    assert db.query_one("SELECT count(*) AS n FROM user_source_groups") == {"n": 2}
