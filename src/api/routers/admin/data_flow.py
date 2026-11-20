"""How postings flow from the sources to each board, counted.

GET /admin/data-flow is the aggregate of api.posting_path: the same gates in
the same order, applied to every posting a window brought in instead of one.
It is built from the predicates the pipeline itself runs (board criteria,
title recipes, the latest closed, clearance and board verdicts), so the page
and the pipeline cannot disagree about why a posting is or is not on a board.

The order is the pipeline's. A posting a title gate or title screen drops is
never verified (api.verification_candidates), so those come before the closed
and clearance checks; checked the other way round, everything a gate dropped
read as "waiting for verification". A posting stops at the first gate it fails.

Nothing records most of these decisions, so the counts are the gates run over
the postings again. On production 2026-11-20 one board's 30 days took 7 to 47
seconds, the location criteria and title regexes over up to 684k postings
being the cost, so answers are served stale (`data_flow_cache_seconds`).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db
from api.auth import AuthedUser
from api.board import criteria as board_criteria
from api.problem import refuse
from api.routers.admin.shared import require_admin
from api.stale_cache import StaleByKey
from core import screening, verdict_reads
from core.catalog.observations import IS_AVAILABLE
from core.screening import Recipe

router = APIRouter()

Window = Literal[24, 168, 720]
SAMPLES = 6

# Every stage a posting can stop at, in pipeline order, with what it means.
# `kind` is how the page draws it: the posting moved on (pass), was left out
# on purpose (drop), or has not been decided yet (wait).
STAGES: dict[str, tuple[str, str]] = {
    "retired": ("drop", "No longer listed by any switched-on source"),
    **{
        f"criteria_{name}": ("drop", f"Outside the criteria: {label}")
        for name, label in {
            "posted_after": "posted before the date the board names",
            "max_age": "older than the board's age window",
            "excluded_locations": "in an excluded location",
            "included_locations": "not in an included location",
            "terms": "no included work term",
        }.items()
    },
    "title_gate": ("drop", "The board's title gate left it out"),
    "title_screen": ("drop", "The title screen for the board's prompt left it out"),
    "awaiting_open_check": (
        "wait",
        "Not verified open yet: queued, or skipped as a title boards never keep",
    ),
    "closed": ("drop", "Verified closed"),
    "awaiting_clearance_check": ("wait", "Clearance and sponsorship not checked yet"),
    "restricted": ("drop", "Needs citizenship or a clearance, or will not sponsor"),
    "not_judged": ("wait", "Not judged by the board's model yet"),
    "rejected": ("drop", "The board's model rejected it"),
    "kept_not_listed": ("wait", "Kept; joins the list at the next projection"),
    "listed": ("pass", "On the board"),
}

_CLOSED = verdict_reads.latest_status("j.url", "closed")
_CLEARANCE = verdict_reads.latest_status("j.url", "clearance")
_VERDICT = verdict_reads.latest_status("j.url", "custom", prompt_hash="%(prompt_hash)s")
_CRITERIA = "".join(
    f"\n  WHEN NOT {condition} THEN 'criteria_{name}'"
    for name, condition in board_criteria.CONDITIONS.items()
)
STAGE_SQL = f"""CASE
  WHEN NOT {IS_AVAILABLE.format(job="j")} THEN 'retired'{_CRITERIA}
  WHEN {screening.skips_sql("%(title_gate)s")} THEN 'title_gate'
  WHEN {screening.skips_sql("%(title_screen)s")} THEN 'title_screen'
  WHEN {_CLOSED} IS DISTINCT FROM 'passed'
    THEN CASE WHEN {_CLOSED} = 'rejected' THEN 'closed' ELSE 'awaiting_open_check' END
  WHEN NOT %(bypass_sponsorship)s AND {_CLEARANCE} IS DISTINCT FROM 'passed'
    THEN CASE WHEN {_CLEARANCE} = 'rejected' THEN 'restricted' ELSE 'awaiting_clearance_check' END
  WHEN {_VERDICT} IS NULL THEN 'not_judged'
  WHEN {_VERDICT} = 'rejected' THEN 'rejected'
  WHEN EXISTS (SELECT 1 FROM managed_board_jobs m
               WHERE m.managed_board_id = %(board_id)s AND m.job_id = j.id) THEN 'listed'
  ELSE 'kept_not_listed' END"""


class Sample(BaseModel):
    job_id: int
    title: str
    company: str
    source: str
    why: str | None = None


class StageCount(BaseModel):
    stage: str
    kind: str
    label: str
    count: int
    samples: list[Sample]


class BoardFlow(BaseModel):
    id: int
    name: str
    model: str
    title_gate: str | None
    title_screen: str | None
    sources: int
    entered: int
    stages: list[StageCount]


class EgressBudget(BaseModel):
    egress_group: str
    host: str
    ok: int
    refused: int
    pace_seconds: float


class Intake(BaseModel):
    sources_on: int
    sources_off: int
    pulls_done: int
    pulls_failed: int
    pulls_deferred_now: int
    listings_fetched: int
    listings_admitted: int
    listings_title_pattern_matched: int
    new_postings: int
    retired: int
    budgets: list[EgressBudget]


class BoardSummary(BaseModel):
    id: int
    name: str
    published: bool
    listed: int


class DataFlow(BaseModel):
    hours: int
    intake: Intake
    boards: list[BoardSummary]
    board: BoardFlow | None


def _intake(hours: int) -> Intake:
    row = db.query_one(
        """
        SELECT
          (SELECT count(*) FILTER (WHERE active) FROM sources) AS sources_on,
          (SELECT count(*) FILTER (WHERE NOT active) FROM sources) AS sources_off,
          p.*,
          (SELECT count(*) FROM tasks WHERE kind = 'ingest_source' AND status = 'pending'
             AND not_before > now()) AS pulls_deferred_now,
          (SELECT count(*) FROM jobs
             WHERE created_at > now() - make_interval(hours => %(hours)s)) AS new_postings
        FROM (
          SELECT count(*) FILTER (WHERE status = 'done') AS pulls_done,
                 count(*) FILTER (WHERE status = 'failed') AS pulls_failed,
                 COALESCE(sum((progress->>'fetched')::int), 0) AS listings_fetched,
                 COALESCE(sum((progress->>'admitted')::int), 0) AS listings_admitted,
                 COALESCE(sum((progress->>'kept')::int), 0) AS listings_title_pattern_matched,
                 COALESCE(sum((progress->>'retired')::int), 0) AS retired
          FROM tasks WHERE kind = 'ingest_source' AND status IN ('done', 'failed')
            AND finished_at > now() - make_interval(hours => %(hours)s)) p
        """,
        {"hours": hours},
    )
    budgets = db.query_as(
        EgressBudget,
        "SELECT egress_group, host, ok, refused, pace_seconds FROM host_budget "
        "WHERE refused > 0 ORDER BY refused DESC, host, egress_group",
    )
    return Intake(**(row or {}), budgets=budgets)


def _board(board_id: int, hours: int) -> BoardFlow | None:
    board = db.query_one(
        "SELECT b.id, b.name, b.requested_model, b.prompt_hash, b.criteria, b.title_gate, "
        "b.bypass_sponsorship_filter, ARRAY(SELECT s.source FROM managed_board_sources s "
        "WHERE s.managed_board_id = b.id) AS sources FROM managed_boards b WHERE b.id = %s",
        (board_id,),
    )
    if board is None:
        return None
    title_gate = (board["title_gate"] or {}).get("recipe")
    title_screen = db.get_config("title_screens").get(board["prompt_hash"])
    params = {
        **screening.PARAMS,
        **board_criteria.params({"criteria": board["criteria"]}),
        "sources": board["sources"],
        "hours": hours,
        "board_id": board_id,
        "prompt_hash": board["prompt_hash"],
        "bypass_sponsorship": board["bypass_sponsorship_filter"],
        "title_gate": title_gate,
        "title_screen": title_screen,
        "samples": SAMPLES,
    }
    rows = db.query(
        f"""
        SELECT stage, count(*) AS count, (array_agg(id ORDER BY id DESC))[1:%(samples)s] AS ids
        FROM (SELECT j.id, {STAGE_SQL} AS stage FROM jobs j
              WHERE j.source = ANY(%(sources)s)
                AND j.created_at > now() - make_interval(hours => %(hours)s)) s
        GROUP BY stage
        """,
        params,
    )
    by_stage = {r["stage"]: r for r in rows}
    samples = _samples([i for r in rows for i in r["ids"]], params)
    stages = [
        StageCount(
            stage=stage,
            kind=kind,
            label=label,
            count=by_stage[stage]["count"],
            samples=[
                _why(samples[i], stage, title_gate, title_screen)
                for i in by_stage[stage]["ids"]
                if i in samples
            ],
        )
        for stage, (kind, label) in STAGES.items()
        if stage in by_stage
    ]
    return BoardFlow(
        id=board["id"],
        name=board["name"],
        model=board["requested_model"],
        title_gate=title_gate,
        title_screen=title_screen,
        sources=len(board["sources"]),
        entered=sum(s.count for s in stages),
        stages=stages,
    )


def _samples(ids: list[int], params: dict) -> dict[int, dict]:
    rows = db.query(
        f"""
        SELECT j.id, j.title, j.company, j.source,
               ({verdict_reads.latest("j.url", "custom", "reason", prompt_hash="%(prompt_hash)s")})
                 AS verdict_reason,
               ({verdict_reads.latest("j.url", "clearance", "reason")}) AS clearance_reason,
               ({verdict_reads.latest("j.url", "closed", "reason")}) AS closed_reason
        FROM jobs j WHERE j.id = ANY(%(ids)s)
        """,
        {**params, "ids": ids},
    )
    return {r["id"]: r for r in rows}


def _why(row: dict, stage: str, title_gate: Recipe | None, title_screen: Recipe | None) -> Sample:
    why = {
        "title_gate": title_gate
        and screening.screen(title_gate, title=row["title"], source=row["source"]).reason,
        "title_screen": title_screen
        and screening.screen(title_screen, title=row["title"], source=row["source"]).reason,
        "rejected": row["verdict_reason"],
        "listed": row["verdict_reason"],
        "kept_not_listed": row["verdict_reason"],
        "restricted": row["clearance_reason"],
        "closed": row["closed_reason"],
    }.get(stage)
    return Sample(
        job_id=row["id"],
        title=row["title"],
        company=row["company"],
        source=row["source"],
        why=why,
    )


def _flow(hours: int, board_id: int | None) -> DataFlow:
    boards = db.query_as(
        BoardSummary,
        "SELECT b.id, b.name, b.published, (SELECT count(*) FROM managed_board_jobs m "
        "WHERE m.managed_board_id = b.id) AS listed FROM managed_boards b ORDER BY b.id",
    )
    return DataFlow(
        hours=hours,
        intake=_intake(hours),
        boards=boards,
        board=_board(board_id, hours) if board_id is not None else None,
    )


_flows: StaleByKey[tuple, DataFlow] = StaleByKey(
    lambda: int(db.get_config("data_flow_cache_seconds"))
)


@router.get("/data-flow")
def data_flow(
    hours: Window = 168,
    board_id: int | None = Query(None),
    user: AuthedUser = Depends(require_admin),
) -> DataFlow:
    """Sources to pulls to postings, and one board's gates, over the window.

    A board's stages count the postings its sources brought in during the
    window, each at the first gate it stops at."""
    if board_id is not None and not db.query_one(
        "SELECT 1 FROM managed_boards WHERE id = %s", (board_id,)
    ):
        raise refuse(404, "NOT_FOUND", "unknown managed board")
    # Keyed on today's screens too: a changed screen must not be answered
    # with the old flow.
    screens = repr(sorted(db.get_config("title_screens").items()))
    return _flows.get((hours, board_id, screens), lambda: _flow(hours, board_id))
