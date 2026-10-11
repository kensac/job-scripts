from __future__ import annotations

from typing import Any

from api import db
from tests.corpus_generator.config import LINKS


class _Schema:
    """The live schema of the database being filled, read once."""

    def __init__(self) -> None:
        self.columns: dict[str, list[dict[str, Any]]] = {}
        self.identity: dict[str, set[str]] = {}
        self.pk: dict[str, list[str]] = {}
        self.fks: dict[tuple[str, str], tuple[str, str]] = {}
        self.unique: dict[str, set[str]] = {}

        for row in db.query(
            "SELECT table_name, column_name, data_type, udt_name, is_nullable, "
            "is_identity, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ("
            "  SELECT table_name FROM information_schema.tables "
            "  WHERE table_schema = 'public' AND table_type = 'BASE TABLE') "
            "ORDER BY table_name, ordinal_position"
        ):
            table = row["table_name"]
            self.columns.setdefault(table, []).append(row)
            if row["is_identity"] == "YES" or (row["column_default"] or "").startswith("nextval"):
                self.identity.setdefault(table, set()).add(row["column_name"])

        for row in db.query(
            """
            SELECT c.conrelid::regclass::text AS child, a.attname AS col,
                   c.confrelid::regclass::text AS parent, f.attname AS pcol,
                   c.contype AS kind
            FROM pg_constraint c
            JOIN unnest(c.conkey) WITH ORDINALITY k(att, i) ON true
            LEFT JOIN unnest(c.confkey) WITH ORDINALITY fk(att, i) ON fk.i = k.i
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.att
            LEFT JOIN pg_attribute f ON f.attrelid = c.confrelid AND f.attnum = fk.att
            WHERE c.contype IN ('f', 'p', 'u')
              AND c.connamespace = 'public'::regnamespace
            """
        ):
            child = row["child"].replace("public.", "")
            if row["kind"] == "f":
                self.fks[(child, row["col"])] = (row["parent"].replace("public.", ""), row["pcol"])
            elif row["kind"] == "p":
                self.pk.setdefault(child, []).append(row["col"])
            else:
                self.unique.setdefault(child, set()).add(row["col"])

    def parent_of(self, table: str, column: str) -> tuple[str, str] | None:
        return self.fks.get((table, column)) or LINKS.get((table, column))


def _order(schema: _Schema, tables: list[str]) -> list[str]:
    """Tables in dependency order. A self-reference is not a cycle - the rows
    are inserted one batch at a time and can point at earlier ones."""
    done: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(
            t
            for t in remaining
            if not {
                parent
                for column in schema.columns[t]
                for parent, _ in [schema.parent_of(t, column["column_name"]) or ("", "")]
                if parent and parent != t and parent in remaining
            }
        )
        if not ready:
            # A genuine cycle. Break it at a table that waits only through
            # nullable columns (email_messages and its current event and
            # match pointers), whose rows can go in first with those columns
            # null. Otherwise take the smallest and let its FKs be null.
            ready = [
                t
                for t in sorted(remaining)
                if all(
                    column["is_nullable"] == "YES"
                    for column in schema.columns[t]
                    for parent, _ in [schema.parent_of(t, column["column_name"]) or ("", "")]
                    if parent and parent != t and parent in remaining
                )
            ][:1] or [sorted(remaining)[0]]
        done.extend(ready)
        remaining -= set(ready)
    return done
