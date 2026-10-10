"""Source selection has one writer: api.source_selection.

Writes to user_sources, managed_board_sources and source_groups were spread
over four routers and the dev seed, each with its own transaction shape.
"""

from __future__ import annotations

import pathlib
import re

from api import db, source_selection

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
_OWNER = _SRC / "api" / "source_selection.py"

_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|DELETE\s+FROM|UPDATE)\s+"
    r"(?:user_sources|managed_board_sources|source_groups)\b",
    re.IGNORECASE,
)


def test_no_selection_write_outside_the_owner():
    copies = [
        f"{path.relative_to(_SRC)}:{text.count(chr(10), 0, m.start()) + 1}"
        for path in sorted(_SRC.rglob("*.py"))
        if path != _OWNER
        for text in [path.read_text()]
        for m in _WRITE.finditer(text)
    ]
    assert not copies, "Write source selection through api.source_selection: " + ", ".join(copies)


def _held(uid: int) -> list[str]:
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
    # "Only these" keeps a held board that is switched off.
    source_selection.join_group(uid, ["b"], only=True)
    assert _held(uid) == ["b", "off"]
    source_selection.join_group(uid, ["a"], only=False)
    assert _held(uid) == ["a", "b", "off"]

    source_selection.save_group("g", ["a", "b"], "bundle", None)
    assert source_selection.save_group("g", None, None, False)["members"] == ["a", "b"]
    source_selection.forget_source("a")
    assert _held(uid) == ["b", "off"]
    assert db.query_one("SELECT members FROM source_groups WHERE name = 'g'") == {"members": ["b"]}
    assert source_selection.delete_group("g")
    assert not source_selection.delete_group("g")
