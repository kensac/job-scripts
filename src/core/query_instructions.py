"""Exact instruction storage, independent of verdict and prompt-report identities."""

from __future__ import annotations

import hashlib
from typing import Any

from psycopg import Connection

from core.pool import connection


class InstructionUnavailable(RuntimeError):
    pass


def intern(conn: Connection[dict[str, Any]], instructions: str) -> int:
    digest = hashlib.sha256(instructions.encode("utf-8")).hexdigest()
    row = conn.execute(
        "SELECT id,instructions FROM ai_instruction_texts WHERE sha256=%s", (digest,)
    ).fetchone()
    if row is None:
        row = conn.execute(
            "INSERT INTO ai_instruction_texts(sha256,instructions) VALUES (%s,%s) "
            "ON CONFLICT(sha256) DO NOTHING RETURNING id,instructions",
            (digest, instructions),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id,instructions FROM ai_instruction_texts WHERE sha256=%s", (digest,)
            ).fetchone()
    if row is None or row["instructions"] != instructions:
        raise InstructionUnavailable("Instruction identity collision or missing content")
    return row["id"]


def hydrate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = [dict(row) for row in rows]
    ids = {
        row["instructions_id"]
        for row in result
        if row.get("instructions") is None and row.get("instructions_id") is not None
    }
    values = {}
    if ids:
        with connection() as conn:
            values = {
                row["id"]: row["instructions"]
                for row in conn.execute(
                    "SELECT id,instructions FROM ai_instruction_texts WHERE id=ANY(%s)",
                    (list(ids),),
                ).fetchall()
            }
    for row in result:
        reference = row.pop("instructions_id", None)
        if row.get("instructions") is None and reference is not None:
            if reference not in values:
                raise InstructionUnavailable("Referenced instructions are unavailable")
            row["instructions"] = values[reference]
    return result
