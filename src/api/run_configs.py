"""The one stored copy of the settings a run executes.

A task payload holds `config_id`, the id of a `run_configs` row, instead of a
copy of its filter or its board's settings. The same settings intern to the
same row, so a filter split into a thousand chunks is stored once.
"""

from __future__ import annotations

from typing import Any

from api import db

FILTER = "filter"
BOARD = "managed_board"

# The board settings a run snapshots. A board run's payload keeps what
# changes per run (revision, resolved model, reservation, execution contract).
BOARD_KEYS = (
    "prompt",
    "prompt_hash",
    "on_ambiguous",
    "fail_closed",
    "bypass_sponsorship_filter",
    "sources",
    "criteria",
)


class RunConfigUnavailable(RuntimeError):
    pass


def intern(kind: str, body: dict[str, Any]) -> int:
    # The digest is over JSONB's own serialization, which orders keys, so a
    # Python dict and the same object copied out of a payload in SQL agree.
    params = {"kind": kind, "body": db.jsonb(body)}
    with db.transaction():
        db.execute(
            "INSERT INTO run_configs(digest,kind,body) "
            "SELECT sha256(convert_to(%(kind)s || ':' || v.body::text,'UTF8')),%(kind)s,v.body "
            "FROM (SELECT %(body)s::jsonb AS body) v ON CONFLICT(digest) DO NOTHING",
            params,
        )
        # A second statement sees a concurrent insert after its unique-key wait.
        # Never resolve a digest collision by substituting the other body.
        row = db.query_one(
            "SELECT c.id,c.kind=%(kind)s AND c.body::text=v.body::text AS exact "
            "FROM run_configs c CROSS JOIN (SELECT %(body)s::jsonb AS body) v "
            "WHERE c.digest=sha256(convert_to(%(kind)s || ':' || v.body::text,'UTF8'))",
            params,
        )
    if row is None or not row["exact"]:
        raise RunConfigUnavailable("Run config digest does not identify its exact body")
    return row["id"]


def _body(config_id: int, kind: str) -> dict[str, Any]:
    row = db.query_one("SELECT body FROM run_configs WHERE id=%s AND kind=%s", (config_id, kind))
    if row is None:
        raise RunConfigUnavailable(f"Run config {config_id} of kind {kind} does not exist")
    return row["body"]


def filter_of(payload: dict[str, Any]) -> dict[str, Any]:
    """The filter a chunk runs. A payload written before config_id holds a copy."""
    if "config_id" in payload:
        return _body(payload["config_id"], FILTER)
    return payload["filter"]


def with_board_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """A board run's payload with its board's settings in place.

    A payload written before config_id holds the settings itself.
    """
    if "config_id" in payload:
        return {**payload, **_body(payload["config_id"], BOARD)}
    return payload
