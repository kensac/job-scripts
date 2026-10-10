"""Source selection has one owner: api.source_selection.

Writes to user_sources, managed_board_sources and source_groups were spread
over four routers and the dev seed, each with its own transaction shape. A
person's sources are read through the view user_source_set, never the picks
table alone, or a followed bundle would not reach them.
"""

from __future__ import annotations

import pathlib
import re

from api import db, source_selection

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
_OWNER = _SRC / "api" / "source_selection.py"
_STORE = _SRC / "core" / "store.py"

_RAW = re.compile(
    r"\b(?:INSERT\s+INTO|DELETE\s+FROM|UPDATE)\s+"
    r"(?:user_sources|user_source_groups|managed_board_sources|source_groups)\b"
    r"|\b(?:FROM|JOIN)\s+user_sources\b",
    re.IGNORECASE,
)


def test_no_selection_sql_outside_the_owner():
    copies = [
        f"{path.relative_to(_SRC)}:{text.count(chr(10), 0, m.start()) + 1}"
        for path in sorted(_SRC.rglob("*.py"))
        if path != _OWNER
        for text in [path.read_text()]
        for m in _RAW.finditer(text)
        # core cannot import api, so the predicate over postings lives there.
        if not (path == _STORE and m.group(0).upper().startswith(("FROM", "JOIN")))
    ]
    assert not copies, "Use api.source_selection or the user_source_set view: " + ", ".join(copies)


def _held(uid: int) -> list[str]:
    return source_selection.held(uid)


def _picks(uid: int) -> list[str]:
    return [
        r["source"]
        for r in db.query(
            "SELECT source FROM user_sources WHERE user_id = %s ORDER BY source", (uid,)
        )
    ]


def test_change_join_replace_and_forget(f):
    uid = f.make_user()
    for name, active in (("a", True), ("b", True), ("off", False)):
        f.make_source(name, active=active)
    source_selection.replace(uid, ["off", "a"])

    assert source_selection.change(uid, ["b", "a"], ["a", "off"]) == (["b"], ["off"])
    assert _held(uid) == ["a", "b"]

    source_selection.replace(uid, ["off", "a"])
    source_selection.save_group("gb", ["b"], "", None)
    source_selection.save_group("ga", ["a"], "", None)
    # "Only these" keeps a held board that is switched off.
    source_selection.join_group(uid, "gb", only=True)
    assert _held(uid) == ["b", "off"]
    source_selection.join_group(uid, "ga", only=False)
    assert _held(uid) == ["a", "b", "off"]

    source_selection.save_group("g", ["a", "b"], "bundle", None)
    assert source_selection.save_group("g", None, None, False)["members"] == ["a", "b"]
    source_selection.forget_source("a")
    assert _held(uid) == ["b", "off"]
    assert db.query_one("SELECT members FROM source_groups WHERE name = 'g'") == {"members": ["b"]}
    assert source_selection.delete_group("g")
    assert not source_selection.delete_group("g")


def test_a_followed_bundle_reaches_members_added_later(f):
    uid = f.make_user()
    for name, active in (("a", True), ("b", True), ("c", True), ("off", False), ("solo", True)):
        f.make_source(name, active=active)
    source_selection.save_group("g", ["a", "off"], "", None)
    source_selection.change(uid, ["solo"], [])
    source_selection.join_group(uid, "g", only=False)
    assert source_selection.followed(uid) == {"g"}

    # A member added later, and one switched on later, reach the follower.
    source_selection.save_group("g", ["a", "b", "off"], None, None)
    assert _held(uid) == ["a", "b", "solo"]
    db.execute("UPDATE sources SET active = true WHERE name = 'off'")
    assert _held(uid) == ["a", "b", "off", "solo"]
    db.execute("UPDATE sources SET active = false WHERE name = 'off'")

    # Leaving one board leaves the bundle and keeps the rest as picks, so
    # the next member added does not arrive.
    assert source_selection.change(uid, [], ["b"]) == ([], ["b"])
    assert source_selection.followed(uid) == set()
    assert _held(uid) == ["a", "solo"]
    source_selection.save_group("g", ["a", "b", "c", "off"], None, None)
    assert _held(uid) == ["a", "solo"]

    # A deleted bundle leaves its followers what it reached.
    source_selection.join_group(uid, "g", only=False)
    assert _held(uid) == ["a", "b", "c", "solo"]
    assert source_selection.delete_group("g")
    assert source_selection.followed(uid) == set()
    assert _held(uid) == _picks(uid) == ["a", "b", "c", "solo"]

    # Saving an exact set ends every follow.
    source_selection.save_group("h", ["a"], "", None)
    source_selection.join_group(uid, "h", only=True)
    source_selection.replace(uid, ["c"])
    assert source_selection.followed(uid) == set()
    assert _held(uid) == ["c"]
