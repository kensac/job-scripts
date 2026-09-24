"""Serialize answer intent without holding a transaction across model calls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from api import db
from api.problem import refuse


def read(user_id: int, fill_id: int, *, lock: bool = False) -> dict[str, Any]:
    row = db.query_one(
        "SELECT id, job_id, fields, submitted_at FROM application_fills "
        "WHERE id = %s AND user_id = %s" + (" FOR UPDATE" if lock else ""),
        (fill_id, user_id),
    )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown fill")
    return row


def _open(row: dict[str, Any]) -> None:
    if row["submitted_at"] is not None:
        raise refuse(409, "FILL_SUBMITTED", "This application was submitted and cannot be edited.")


def _write(row: dict[str, Any]) -> None:
    db.execute(
        "UPDATE application_fills SET fields = %s WHERE id = %s",
        (db.jsonb(row["fields"]), row["id"]),
    )


def _history(field: dict[str, Any], kind: str, **values: Any) -> None:
    field.setdefault("answer_history", []).append(
        {"kind": kind, "at": datetime.now(UTC).isoformat(), **values}
    )


def edit(user_id: int, fill_id: int, key: str, revision: int, value: str, feedback: str) -> dict:
    with db.transaction():
        row = read(user_id, fill_id, lock=True)
        _open(row)
        field = next((f for f in row["fields"] if f["key"] == key), None)
        if field is None:
            raise refuse(404, "NOT_FOUND", "unknown field")
        if field.get("answer_revision", 0) != revision:
            raise refuse(409, "ANSWER_CHANGED", "The answer changed. Reload it before saving.")
        field["answer_revision"] = revision + 1
        field.pop("answer_generation", None)
        field["review_value"] = value
        field["feedback"] = feedback
        _history(field, "edit", value=value, feedback=feedback)
        _write(row)
        return field


def reserve(user_id: int, fill_id: int, keys: list[str], job_id: int | None) -> tuple[str, dict]:
    with db.transaction():
        row = read(user_id, fill_id, lock=True)
        _open(row)
        if row["job_id"] != job_id:
            raise refuse(409, "FILL_JOB_CHANGED", "The request does not match this application.")
        by_key = {f["key"]: f for f in row["fields"]}
        if len(set(keys)) != len(keys) or any(k not in by_key for k in keys):
            raise refuse(422, "INVALID_FIELDS", "Fields must be unique and belong to this fill.")
        token = uuid4().hex
        for key in keys:
            by_key[key]["answer_generation"] = token
        _write(row)
        return token, {key: dict(by_key[key]) for key in keys}


def complete(
    user_id: int,
    fill_id: int,
    token: str,
    keys: list[str],
    answers: dict,
    raw: dict,
    skipped: list[str],
    *,
    review_only: bool,
) -> bool:
    with db.transaction():
        row = read(user_id, fill_id, lock=True)
        by_key = {f["key"]: f for f in row["fields"]}
        if row["submitted_at"] is not None or any(
            by_key.get(key, {}).get("answer_generation") != token for key in keys
        ):
            return False
        for key in keys:
            field = by_key[key]
            field.pop("answer_generation", None)
            if key in raw:
                field["ai_answer"] = raw[key]
                _history(field, "generated", value=raw[key], feedback=field.get("feedback", ""))
            if key in answers:
                if review_only:
                    field["review_value"] = answers[key]
                else:
                    field.update(rung="ai", value=answers[key])
            if key in skipped:
                field["never_ai"] = True
            field["answer_revision"] = field.get("answer_revision", 0) + 1
        _write(row)
        return True
