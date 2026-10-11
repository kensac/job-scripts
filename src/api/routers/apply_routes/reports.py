"""Application fill ledger, captured page reports, and coverage summary."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api import apply, db
from api.auth import AuthedUser, require_user
from api.problem import SIZE_REFUSALS, refuse
from api.routers.apply_routes.events import record_apply_event
from api.routers.apply_routes.models import (
    ApplyReport,
    Fill,
    FillList,
    LabelCount,
    ProblemReport,
    ProblemReportDetail,
    ReportCreated,
    ReportList,
)

router = APIRouter()


class PageReport(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    note: str = Field(default="", max_length=4000)
    page: dict[str, Any]


MAX_REPORT_BYTES = 2_000_000


@router.post("/user/apply/reports", status_code=201, responses=SIZE_REFUSALS)
def create_report(body: PageReport, user: AuthedUser = Depends(require_user)) -> ReportCreated:
    """Accept reports from older extensions that send one JSON request."""
    if len(json.dumps(body.page)) > MAX_REPORT_BYTES:
        raise refuse(413, "REPORT_TOO_LARGE", "the page capture is over 2 MB")
    report = db.query_one_as(
        ReportCreated,
        "INSERT INTO application_reports (user_id, url, host, note, page) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at",
        (user.id, body.url, urlsplit(body.url).netloc, body.note.strip(), db.jsonb(body.page)),
    )
    assert report is not None  # an insert with RETURNING always yields its row
    # The one event that says a reader did not work on a real page. The note is
    # a person's own words about their own application, so only whether there
    # was one ships; the host is what says where to look.
    record_apply_event(
        "apply_page_reported",
        user,
        host=urlsplit(body.url).netloc,
        has_note=bool(body.note.strip()),
    )
    return report


@router.get("/user/apply/reports")
def list_reports(limit: int = 50, user: AuthedUser = Depends(require_user)) -> ReportList:
    return ReportList(
        reports=db.query_as(
            ProblemReport,
            "SELECT r.id, r.url, r.host, r.note, r.created_at, "
            "COALESCE(r.page->>'title', u.title) AS title, "
            "COALESCE(jsonb_array_length(r.page->'fields'), u.field_count, 0) AS fields "
            "FROM application_reports r LEFT JOIN application_report_uploads u ON u.report_id = r.id "
            "WHERE r.user_id = %s ORDER BY r.created_at DESC LIMIT %s",
            (user.id, max(1, min(limit, 500))),
        )
    )


@router.get("/user/apply/reports/{report_id}")
def get_report(report_id: int, user: AuthedUser = Depends(require_user)) -> ProblemReportDetail:
    row = db.query_one_as(
        ProblemReportDetail,
        "SELECT r.id, r.url, r.host, r.note, r.page, r.created_at, u.chunk_count "
        "FROM application_reports r LEFT JOIN application_report_uploads u ON u.report_id = r.id "
        "WHERE r.id = %s AND r.user_id = %s",
        (report_id, user.id),
    )
    if not row:
        raise refuse(404, "NOT_FOUND", "unknown report")
    return row


@router.get("/user/apply/fills")
def list_fills(limit: int = 50, user: AuthedUser = Depends(require_user)) -> FillList:
    return FillList(
        fills=db.query_as(
            Fill,
            "SELECT f.id, f.job_id, f.url, f.host, f.created_at, f.submitted_at, "
            "j.company, j.title, jsonb_array_length(f.fields) AS fields "
            "FROM application_fills f LEFT JOIN jobs j ON j.id = f.job_id "
            "WHERE f.user_id = %s ORDER BY f.created_at DESC LIMIT %s",
            (user.id, max(1, min(limit, 500))),
        )
    )


@router.get("/user/apply/report")
def report(user: AuthedUser = Depends(require_user)) -> ApplyReport:
    """How the ladder is doing on this person's submitted forms: fields by
    rung, how many the person changed, and the labels most often left
    blank or corrected. The blank list is the backlog: each label on it
    is a rule to add or a fact the profile is missing."""
    rows = db.query(
        """
        SELECT f.host, e.value->>'label' AS label, e.value->>'kind' AS kind,
               COALESCE(e.value->>'rung', '') AS rung,
               COALESCE((e.value->>'changed')::boolean, false) AS changed
        FROM application_fills f, jsonb_array_elements(f.fields) e
        WHERE f.user_id = %s AND f.submitted_at IS NOT NULL
        """,
        (user.id,),
    )
    by_rung: dict[str, int] = {}
    blank: dict[str, int] = {}
    corrected: dict[str, int] = {}
    unchanged = 0
    for r in rows:
        by_rung[r["rung"] or "blank"] = by_rung.get(r["rung"] or "blank", 0) + 1
        norm = apply.normalize(r["label"] or "")
        if not r["rung"]:
            blank[norm] = blank.get(norm, 0) + 1
        elif r["changed"]:
            corrected[norm] = corrected.get(norm, 0) + 1
        else:
            unchanged += 1

    def top(counts: dict[str, int]) -> list[LabelCount]:
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:25]
        return [LabelCount(label=k, count=v) for k, v in ranked]

    submitted = db.query_one(
        "SELECT count(*) AS n FROM application_fills WHERE user_id = %s AND submitted_at IS NOT NULL",
        (user.id,),
    )
    return ApplyReport(
        forms_submitted=(submitted or {}).get("n", 0),
        fields=len(rows),
        filled_unchanged=unchanged,
        by_rung=by_rung,
        most_often_blank=top(blank),
        most_often_corrected=top(corrected),
    )
