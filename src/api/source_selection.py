"""The one writer of source selection.

Three tables say which sources feed whom: `user_sources` (the boards a person
picked), `managed_board_sources` (the boards a managed board reads) and
`source_groups.members` (the boards a bundle offers). Every insert, update
and delete against them is here; `tests/test_source_selection_owner.py`
fails on one anywhere else.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from api import db

# ---- a person's picks ----


def change(user_id: int, add: Iterable[str], remove: Iterable[str]) -> tuple[list[str], list[str]]:
    """Subscribe to `add`, unsubscribe from `remove`, in one transaction. A
    name in both ends up subscribed. Returns what the write touched: a name
    already held is not "added"."""
    adding = sorted(set(add))
    removing = sorted(set(remove) - set(adding))
    with db.transaction():
        removed = db.query(
            "DELETE FROM user_sources WHERE user_id = %s AND source = ANY(%s) RETURNING source",
            (user_id, removing),
        )
        added = db.query(
            "INSERT INTO user_sources (user_id, source) SELECT %s, unnest(%s::text[]) "
            "ON CONFLICT DO NOTHING RETURNING source",
            (user_id, adding),
        )
    return sorted(r["source"] for r in added), sorted(r["source"] for r in removed)


def replace(user_id: int, names: Iterable[str]) -> None:
    """The person holds exactly `names`."""
    with db.transaction():
        db.execute("DELETE FROM user_sources WHERE user_id = %s", (user_id,))
        db.execute(
            "INSERT INTO user_sources (user_id, source) SELECT %s, unnest(%s::text[])",
            (user_id, sorted(set(names))),
        )


def join_group(user_id: int, members: Iterable[str], *, only: bool) -> None:
    """Take a bundle's boards. `only` first drops every board on offer the
    person holds; a held board that is switched off is not on offer, so it
    is kept, the same as a save from the page keeps it."""
    with db.transaction():
        if only:
            db.execute(
                "DELETE FROM user_sources WHERE user_id = %s "
                "AND source IN (SELECT name FROM sources WHERE active)",
                (user_id,),
            )
        db.execute(
            "INSERT INTO user_sources (user_id, source) SELECT %s, unnest(%s::text[]) "
            "ON CONFLICT DO NOTHING",
            (user_id, sorted(set(members))),
        )


# ---- managed boards ----


def set_board_sources(board_id: int, sources: list[str]) -> None:
    """The board reads exactly `sources`. Runs inside the caller's
    transaction, which holds the board row."""
    db.execute("DELETE FROM managed_board_sources WHERE managed_board_id = %s", (board_id,))
    if sources:
        db.executemany(
            "INSERT INTO managed_board_sources (managed_board_id, source) VALUES (%s, %s)",
            [(board_id, source) for source in sources],
        )


# ---- bundles ----


def save_group(
    name: str, members: list[str] | None, description: str | None, active: bool | None
) -> dict[str, Any]:
    """Create or update a bundle. A field that is None is left as it is."""
    row = db.query_one(
        """
        INSERT INTO source_groups (name, members, description, active)
        VALUES (%(name)s, COALESCE(%(members)s::text[], '{}'), COALESCE(%(description)s, ''),
                COALESCE(%(active)s, TRUE))
        ON CONFLICT (name) DO UPDATE SET
            members = COALESCE(%(members)s::text[], source_groups.members),
            description = COALESCE(%(description)s, source_groups.description),
            active = COALESCE(%(active)s, source_groups.active)
        RETURNING name, members, description, active, created_at
        """,
        {"name": name, "members": members, "description": description, "active": active},
    )
    assert row is not None  # an upsert with RETURNING returns its row
    return row


def delete_group(name: str) -> bool:
    return (
        db.query_one("DELETE FROM source_groups WHERE name = %s RETURNING name", (name,))
        is not None
    )


# ---- a source leaving the catalog ----


def forget_source(name: str) -> None:
    """Take a source out of every selection before it is deleted. Runs inside
    the caller's transaction."""
    db.execute("DELETE FROM user_sources WHERE source = %s", (name,))
    db.execute(
        "UPDATE source_groups SET members = array_remove(members, %s) WHERE %s = ANY(members)",
        (name, name),
    )
    db.execute("DELETE FROM managed_board_sources WHERE source = %s", (name,))
