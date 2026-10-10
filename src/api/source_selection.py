"""The one owner of source selection.

Four tables say which sources feed whom: `user_sources` (the boards a person
picked one by one), `user_source_groups` (the bundles a person follows),
`managed_board_sources` (the boards a managed board reads) and
`source_groups.members` (the boards a bundle offers). Every insert, update
and delete against them is here; `tests/test_source_selection_owner.py`
fails on one anywhere else, and on a read of `user_sources` too.

A person's sources are the view `user_source_set`: their picks plus every
active member of every bundle they follow, so a member added to a bundle
later reaches its followers (Kanishk, 2026-10-10). Readers read the view.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from api import db


def held(user_id: int) -> list[str]:
    """Every source that reaches the person, sorted."""
    return [
        r["source"]
        for r in db.query(
            "SELECT source FROM user_source_set WHERE user_id = %s ORDER BY source", (user_id,)
        )
    ]


def followed(user_id: int) -> set[str]:
    return {
        r["group_name"]
        for r in db.query(
            "SELECT group_name FROM user_source_groups WHERE user_id = %s", (user_id,)
        )
    }


def _lock(user_id: int) -> None:
    # Two saves from one person read the set, then write it; the person's
    # row serializes them, as it does for saved views.
    db.query_one("SELECT id FROM users WHERE id = %s FOR NO KEY UPDATE", (user_id,))


def _unfollow(user_id: int, groups_sql: str, params: dict[str, Any]) -> None:
    """Stop following the bundles `groups_sql` names, keeping what each
    reached as picks. Leaving one board of a followed bundle is leaving the
    bundle: the bundle is only theirs while it reaches them whole."""
    db.execute(
        f"""
        WITH dropped AS (
            DELETE FROM user_source_groups
            WHERE user_id = %(uid)s AND group_name IN ({groups_sql})
            RETURNING group_name
        )
        INSERT INTO user_sources (user_id, source)
        SELECT %(uid)s, s.name FROM dropped d
        JOIN source_groups g ON g.name = d.group_name
        CROSS JOIN LATERAL unnest(g.members) AS m(name)
        JOIN sources s ON s.name = m.name AND s.active
        ON CONFLICT DO NOTHING
        """,
        {"uid": user_id, **params},
    )


def change(user_id: int, add: Iterable[str], remove: Iterable[str]) -> tuple[list[str], list[str]]:
    """Subscribe to `add`, unsubscribe from `remove`, in one transaction. A
    name in both ends up subscribed. Returns what the write changed in the
    person's set: a name already held is not "added"."""
    adding = sorted(set(add))
    removing = sorted(set(remove) - set(adding))
    touched = set(adding) | set(removing)
    with db.transaction():
        _lock(user_id)
        before = touched & set(held(user_id))
        _unfollow(
            user_id,
            "SELECT g.name FROM source_groups g CROSS JOIN LATERAL unnest(g.members) AS m(name) "
            "JOIN sources s ON s.name = m.name AND s.active WHERE m.name = ANY(%(remove)s::text[])",
            {"remove": removing},
        )
        db.execute(
            "DELETE FROM user_sources WHERE user_id = %s AND source = ANY(%s)",
            (user_id, removing),
        )
        db.execute(
            "INSERT INTO user_sources (user_id, source) SELECT %s, unnest(%s::text[]) "
            "ON CONFLICT DO NOTHING",
            (user_id, adding),
        )
        after = touched & set(held(user_id))
    return sorted(after - before), sorted(before - after)


def replace(user_id: int, names: Iterable[str]) -> None:
    """The person holds exactly `names`, picked one by one."""
    with db.transaction():
        _lock(user_id)
        db.execute("DELETE FROM user_source_groups WHERE user_id = %s", (user_id,))
        db.execute("DELETE FROM user_sources WHERE user_id = %s", (user_id,))
        db.execute(
            "INSERT INTO user_sources (user_id, source) SELECT %s, unnest(%s::text[])",
            (user_id, sorted(set(names))),
        )


def join_group(user_id: int, group: str, *, only: bool) -> None:
    """Follow a bundle. `only` first drops every board on offer the person
    holds and every bundle they follow; a held board that is switched off is
    not on offer, so it is kept, the same as a save from the page keeps it."""
    with db.transaction():
        _lock(user_id)
        if only:
            db.execute("DELETE FROM user_source_groups WHERE user_id = %s", (user_id,))
            db.execute(
                "DELETE FROM user_sources WHERE user_id = %s "
                "AND source IN (SELECT name FROM sources WHERE active)",
                (user_id,),
            )
        db.execute(
            "INSERT INTO user_source_groups (user_id, group_name) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (user_id, group),
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
    """Delete a bundle. Its followers keep what it reached, as picks: a
    bundle going away is not the person leaving those boards."""
    with db.transaction():
        for r in db.query(
            "SELECT user_id FROM user_source_groups WHERE group_name = %s ORDER BY user_id",
            (name,),
        ):
            _unfollow(r["user_id"], "%(group)s", {"group": name})
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
