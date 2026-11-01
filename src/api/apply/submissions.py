"""Which fill a submit belongs to.

Before #1268, a Greenhouse dropdown that removed a hidden input made the
extension think the form had moved to a new page. It dropped the fill it had
made and, on Submit, opened a new fill with no fields and submitted that. The
submission belongs to the earlier fill on the same url, the one holding what
was filled.

A zero-field fill whose submission moved names the fill it moved to in
`superseded_by` and keeps no `submitted_at` or `application_id` of its own.
Every reader counts submitted fills by `submitted_at`, so each counts the
submission once, on the fill that has the fields. The row itself stays.
"""

from __future__ import annotations

from typing import Any

from api import db


def _filled_sibling(user_id: int, url: str, before_id: int) -> int | None:
    """The newest earlier open fill with fields for this person and url."""
    row = db.query_one(
        "SELECT id FROM application_fills WHERE user_id = %s AND url = %s AND id < %s "
        "AND submitted_at IS NULL AND jsonb_array_length(fields) > 0 "
        "ORDER BY id DESC LIMIT 1 FOR UPDATE",
        (user_id, url, before_id),
    )
    return row["id"] if row else None


def target(user_id: int, fill: dict[str, Any]) -> int:
    """The fill a submit of `fill` records on. The caller holds `fill`
    locked, with its id, url, fields, submitted_at and superseded_by.

    A zero-field open fill with a filled open sibling is marked superseded
    by that sibling, and the sibling is the target."""
    if fill["superseded_by"] is not None:
        return fill["superseded_by"]
    if fill["submitted_at"] is not None or fill["fields"]:
        return fill["id"]
    sibling = _filled_sibling(user_id, fill["url"], fill["id"])
    if sibling is None:
        return fill["id"]
    db.execute(
        "UPDATE application_fills SET superseded_by = %s WHERE id = %s", (sibling, fill["id"])
    )
    return sibling


def repair() -> list[tuple[int, int]]:
    """Move each zero-field submission recorded before #1268 onto its filled
    sibling, as `target` does at submit time. Returns (empty, filled) pairs.

    The empty fill's submit read no fields, so it holds no finals: the
    filled fill's fields get none, which readers take as unknown rather
    than unchanged. A zero-field submission with no open filled sibling is
    left as it is. Idempotent: a moved fill has no submitted_at, so a
    second run selects nothing."""
    moved: list[tuple[int, int]] = []
    with db.transaction():
        empties = db.query(
            "SELECT id, user_id, url, submitted_at, application_id FROM application_fills "
            "WHERE submitted_at IS NOT NULL AND superseded_by IS NULL "
            "AND jsonb_array_length(fields) = 0 ORDER BY id FOR UPDATE"
        )
        for empty in empties:
            sibling = _filled_sibling(empty["user_id"], empty["url"], empty["id"])
            if sibling is None:
                continue
            db.execute(
                "UPDATE application_fills SET submitted_at = %s, application_id = %s WHERE id = %s",
                (empty["submitted_at"], empty["application_id"], sibling),
            )
            db.execute(
                "UPDATE application_fills SET submitted_at = NULL, application_id = NULL, "
                "superseded_by = %s WHERE id = %s",
                (sibling, empty["id"]),
            )
            moved.append((empty["id"], sibling))
    return moved
