"""5a7c2e9d1b40 converts prefs.views into saved_views the way the frontend's
copy would, and only on pages that copy has not reached.

The expected states are what lib/saved-views.ts paramsToState produces for
the same params (personal-portfolio, origin/main 2026-10-10).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

from api import db

_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "5a7c2e9d1b40_prefs_views_to_saved_views.py"
)


def _upgrade() -> None:
    spec = importlib.util.spec_from_file_location("prefs_views_migration", _PATH)
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


def _views(uid: int) -> list[tuple]:
    return [
        (r["page"], r["name"], r["state"], r["is_default"], r["position"])
        for r in db.query(
            "SELECT page, name, state, is_default, position FROM saved_views "
            "WHERE user_id = %s ORDER BY page, position",
            (uid,),
        )
    ]


def test_converts_only_pages_without_saved_views(f):
    uid = f.make_user()
    prefs = {
        "views": {
            "board": [
                {"name": "Kept", "params": {"statuses": "not_applied"}},
            ],
            "pipeline": [
                {
                    "name": " Mine ",
                    "params": {
                        "sort": "date_posted,added_at",
                        "dir": "asc,desc",
                        "q": "rust",
                        "columns": '[{"colId": "company"}]',
                        "column_filters": '[{"a": "x, y"}]',
                        "ats": "greenhouse, lever,",
                        "empty": "",
                    },
                    "is_default": True,
                },
                {"name": "Second", "params": {}, "is_default": True},
                {"name": "", "params": {"x": "1"}},
                {"name": "No params"},
            ],
        },
        "auto_draft": False,
    }
    db.execute("INSERT INTO user_settings (user_id, prefs) VALUES (%s, %s)", (uid, db.jsonb(prefs)))
    # The board page was read once already: its row is the copy, edited since.
    db.execute(
        "INSERT INTO saved_views (user_id, page, name, state) VALUES (%s, 'board', 'Kept', %s)",
        (uid, db.jsonb({"filters": {}, "sorts": []})),
    )

    _upgrade()
    after_first = _views(uid)
    _upgrade()

    assert (
        _views(uid)
        == after_first
        == [
            ("board", "Kept", {"filters": {}, "sorts": []}, False, 0),
            (
                "pipeline",
                "Mine",
                {
                    "filters": {
                        "ats": ["greenhouse", "lever"],
                        "column_filters": ['[{"a": "x, y"}]'],
                    },
                    "sorts": [
                        {"key": "date_posted", "dir": "asc"},
                        {"key": "added_at", "dir": "desc"},
                    ],
                    "search": "rust",
                    "columns": [{"colId": "company"}],
                },
                True,
                0,
            ),
            ("pipeline", "Second", {"filters": {}, "sorts": []}, False, 1),
        ]
    )
