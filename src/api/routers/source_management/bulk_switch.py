"""Bulk changes to source activity and ingest interval."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import db
from api.auth import AuthedUser
from api.problem import refuse
from api.routers.admin import require_admin
from core.fetching import boards

router = APIRouter()


class SourcesSwitched(BaseModel):
    """What was asked for, what the selection resolved to, and which rows
    actually moved. `selected` and `changed` differ by the rows that already
    held the value, and by copies of a board another active source reads,
    which a switch on leaves off."""

    active: bool | None
    ingest_interval_hours: int | None
    selected: list[str]
    changed: list[str]


class SourceSwitchBody(BaseModel):
    # What to set on the selection: the on/off flag, the pull interval, or
    # both. At least one.
    active: bool | None = None
    ingest_interval_hours: int | None = Field(default=None, ge=1, le=168)
    # Any combination; the selection is their union. A kind is the board format
    # read off the listings URL (core/boards.kind), a group is a source bundle.
    kind: str | None = None
    group: str | None = None
    names: list[str] | None = None


@router.post("/sources/switch")
def switch_sources(
    body: SourceSwitchBody, user: AuthedUser = Depends(require_admin)
) -> SourcesSwitched:
    """One write sets a whole category of boards: on or off, and how often
    they are pulled.

    sources.active is already the switch that stops both the scrape and the AI
    spend on a board's postings (SUBSCRIBED_SOURCE in core/store.py), and
    ingest_interval_hours is what the scheduler reads, so a category is a way
    of SELECTING rows for those writes, not a second layer of state. Every row
    shows its own values afterwards, and nothing is overridden silently. The
    top level is the format (all Workday boards), the level below is a bundle
    (the quant firms), and names catch the rest.
    """
    if body.kind is None and body.group is None and not body.names:
        raise refuse(400, "NO_SELECTION", "give a kind, a group, or names")
    if body.active is None and body.ingest_interval_hours is None:
        raise refuse(400, "NOTHING_TO_SET", "give active, ingest_interval_hours, or both")
    selected_sources: set[str] = set(body.names or [])
    if body.group is not None:
        source_group = db.query_one(
            "SELECT members FROM source_groups WHERE name = %s", (body.group,)
        )
        if not source_group:
            raise refuse(404, "NOT_FOUND", "unknown group")
        selected_sources |= set(source_group["members"])
    if body.kind is not None:
        selected_sources |= {
            source_row["name"]
            for source_row in db.query("SELECT name, listings_url FROM sources")
            if boards.kind(source_row["listings_url"]) == body.kind
        }
    # Switching on never makes a second active reader of one board (see
    # catalog.check_one_active_per_board): a selected copy of a board that an
    # active source, or an earlier-created selected one, already reads stays
    # off. A bundle holds a switched-off copy beside its keeper.
    copies = (
        [
            row["name"]
            for row in db.query(
                "SELECT s.name FROM sources s WHERE s.name = ANY(%(names)s) AND NOT s.active "
                "AND EXISTS (SELECT 1 FROM sources o WHERE o.name <> s.name "
                "AND lower(o.listings_url) = lower(s.listings_url) AND (o.active "
                "OR (o.name = ANY(%(names)s) AND (o.created_at, o.name) < (s.created_at, s.name))))",
                {"names": sorted(selected_sources)},
            )
        ]
        if body.active
        else []
    )
    fields_to_set = {
        column: value
        for column, value in (
            ("active", body.active),
            ("ingest_interval_hours", body.ingest_interval_hours),
        )
        if value is not None
    }
    target = {column: f"%({column})s" for column in fields_to_set}
    if copies:
        target["active"] = "CASE WHEN name = ANY(%(copies)s) THEN active ELSE %(active)s END"
    changed = db.query(
        "UPDATE sources SET "
        + ", ".join(f"{column} = {value}" for column, value in target.items())
        + " WHERE name = ANY(%(names)s) AND ("
        + " OR ".join(f"{column} IS DISTINCT FROM {value}" for column, value in target.items())
        + ") RETURNING name",
        {**fields_to_set, "names": sorted(selected_sources), "copies": copies},
    )
    return SourcesSwitched(
        active=body.active,
        ingest_interval_hours=body.ingest_interval_hours,
        selected=sorted(selected_sources),
        changed=sorted(r["name"] for r in changed),
    )
