"""The For you page: recent tech postings a person has not touched, in the
order of their taste model, and the postings nearest to one posting.

Open to admins only. It ranks the whole catalog past the person's board, and
seeing a posting that is not on one's board is something visibility grants
administrators alone (docs/agents/visibility.md); a person acts on a posting
from here through the board's own row writer, PATCH /user/jobs/{id}, which
already lets anyone pin a public catalog posting.
"""

from __future__ import annotations

import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api import db, for_you
from api.auth import AuthedUser
from api.connections import MAX_FIRST, PageInfo, cursor_for, decode_cursor
from api.problem import refuse
from api.queue import enqueue
from api.routers.admin.shared import require_admin
from core import recommendations, taste

router = APIRouter()

# A page the client asks for when it names none: about a screen of cards.
DEFAULT_FIRST = 25


class Pay(BaseModel):
    min: int | None
    max: int | None
    period: str | None
    currency: str | None


class LikeApplication(BaseModel):
    """The application a posting is nearest to, and how near (cosine)."""

    job_id: int
    title: str
    company: str
    similarity: float


class Copy(BaseModel):
    """Another listing of the same job: reposted, in another city, on
    another board."""

    job_id: int
    url: str
    company: str
    title: str
    locations: list[str]


class Recommendation(BaseModel):
    job_id: int
    url: str
    company: str
    title: str
    locations: list[str]
    posted: datetime.datetime | None
    pay: Pay | None
    # The share of the person's pool this posting ranks above.
    match: float
    like: LikeApplication | None
    # A near copy of something the person applied to (core.recommendations.NEAR_COPY).
    applied_copy: bool
    # What the person's enabled filters said: every one kept it, or not all
    # of them have judged it yet; None when they have none switched on.
    filter: Literal["kept", "unchecked"] | None
    # Its near copies ranked below it in the pool, folded into this row: a
    # posting with a near copy ranked above it is never a row of its own.
    copies: list[Copy]


class TasteModel(BaseModel):
    id: int
    trained_at: datetime.datetime
    applied: int
    held_out_roc_auc: float
    held_out_in_top_share: float
    top_share: float


class Recommendations(BaseModel):
    nodes: list[Recommendation]
    page_info: PageInfo
    total_count: int
    model: TasteModel | None
    # A model is being fitted: there is none, or the one shown is a day old.
    training: bool
    # Why today's fit failed, when it did; the page asks again tomorrow.
    fit_failed: str | None = None


class SimilarPosting(BaseModel):
    job_id: int
    url: str
    company: str
    title: str
    locations: list[str]
    posted: datetime.datetime | None
    pay: Pay | None
    similarity: float


class SimilarPostings(BaseModel):
    nodes: list[SimilarPosting]
    page_info: PageInfo
    total_count: int


def _pay(row: dict[str, Any]) -> Pay | None:
    if row["comp_min"] is None and row["comp_max"] is None:
        return None
    return Pay(
        min=row["comp_min"],
        max=row["comp_max"],
        period=row["comp_period"],
        currency=row["comp_currency"],
    )


def _first(first: int | None) -> int:
    if first is not None and not 0 < first <= MAX_FIRST:
        raise refuse(400, "INVALID_FIRST", f"first must be between 1 and {MAX_FIRST}")
    return first or DEFAULT_FIRST


def _model(user_id: int) -> tuple[TasteModel | None, bool, str | None]:
    """The person's taste model, whether a fit is under way, and why today's
    failed when it did. A fit is asked for when they have none, or theirs was
    fitted before today (UTC), the day the schedule refits on; one fit a
    person a day, so a failed one stands until tomorrow."""
    row = db.query_one(taste.latest_sql(), {"user_id": user_id})
    today = datetime.datetime.now(datetime.UTC).date()
    stale = row is None or row["trained_at"].astimezone(datetime.UTC).date() < today
    failed = None
    if stale:
        key = f"train-taste:{user_id}:{today.isoformat()}"
        enqueue("train_taste", {"cycle": today.isoformat(), "user_id": user_id}, dedupe_key=key)
        fit = db.query_one("SELECT status, error FROM tasks WHERE dedupe_key = %s", (key,))
        if fit and fit["status"] == "failed":
            failed = fit["error"] or "the fit failed"
            stale = False
    if row is None:
        return None, stale, failed
    evaluation = row["evaluation"]
    return (
        TasteModel(
            id=row["id"],
            trained_at=row["trained_at"],
            applied=row["examples"]["applied"],
            held_out_roc_auc=evaluation["held_out_roc_auc"],
            held_out_in_top_share=evaluation["held_out_in_top_share"],
            top_share=evaluation["top_share"],
        ),
        stale,
        failed,
    )


def _ranking(user_id: int, model: TasteModel) -> bool:
    """Ask for the person's ranking to be computed when it was not computed
    with their latest model; whether it is being computed."""
    run = db.query_one("SELECT model_id FROM recommendation_runs WHERE user_id = %s", (user_id,))
    if run is not None and run["model_id"] == model.id:
        return False
    enqueue(
        "rank_for_you",
        {"user_id": user_id, "model_id": model.id},
        dedupe_key=f"rank-for-you:{user_id}:model-{model.id}",
    )
    return True


@router.get("/user/recommendations")
def recommendations_page(
    first: int | None = Query(None),
    after: str | None = Query(None),
    kept_only: bool = Query(False),
    user: AuthedUser = Depends(require_admin),
) -> Recommendations:
    """The person's recent tech postings they have not touched, held to their
    board's conditions (api.for_you.GATE), best match first: each with the
    application it is nearest to, whether it is a near copy of one, what
    their enabled filters said, and its own near copies folded in. Read from
    the person's computed ranking (tasks.for_you); total_count counts its
    rows."""
    limit = _first(first)
    model, training, fit_failed = _model(user.id)
    empty = Recommendations(
        nodes=[],
        page_info=PageInfo(end_cursor=None, has_next_page=False),
        total_count=0,
        model=model,
        training=training,
        fit_failed=fit_failed,
    )
    if model is None:
        return empty
    training = _ranking(user.id, model) or training
    run = db.query_one("SELECT run_id FROM recommendation_runs WHERE user_id = %s", (user.id,))
    if run is None:
        return empty.model_copy(update={"training": True})
    query = {"q": "recommendations", "run": run["run_id"], "kept_only": kept_only}
    after_rank = decode_cursor(after, query) if after else None
    params = {"user_id": user.id, "kept_only": kept_only, "after_rank": after_rank}
    rows = db.query(for_you.PAGE, {**params, "limit": limit + 1})
    more = len(rows) > limit
    rows = rows[:limit]
    ids = [r["id"] for r in rows]
    copies: dict[int, list[Copy]] = {}
    for c in db.query(for_you.COPIES, {**params, "ids": ids}) if ids else []:
        copies.setdefault(c["copy_of"], []).append(
            Copy(
                job_id=c["id"],
                url=c["url"],
                company=c["company"],
                title=c["title"],
                locations=c["locations"] or [],
            )
        )
    urls = [r["url"] for r in rows]
    near = (
        {
            r["url"]: r
            for r in db.query(
                recommendations.nearest_application_sql(), {"urls": urls, "user_id": user.id}
            )
        }
        if urls
        else {}
    )
    has_filter = for_you.has_filter(user.id)
    nodes = []
    for r in rows:
        like = near.get(r["url"])
        nodes.append(
            Recommendation(
                job_id=r["id"],
                url=r["url"],
                company=r["company"],
                title=r["title"],
                locations=r["locations"] or [],
                posted=r["posted"],
                pay=_pay(r),
                match=1 - (r["rank"] - 1) / r["postings"],
                like=LikeApplication(
                    job_id=like["job_id"],
                    title=like["title"],
                    company=like["company"],
                    similarity=float(like["similarity"]),
                )
                if like
                else None,
                applied_copy=bool(like and like["similarity"] >= recommendations.NEAR_COPY),
                filter=("kept" if r["kept"] else "unchecked") if has_filter else None,
                copies=copies.get(r["id"], []),
            )
        )
    total = db.query_one(for_you.ROWS, params)
    return Recommendations(
        nodes=nodes,
        page_info=PageInfo(
            end_cursor=cursor_for(query, rows[-1]["rank"]) if rows else None,
            has_next_page=more,
        ),
        total_count=total["n"] if total else 0,
        model=model,
        training=training,
        fit_failed=fit_failed,
    )


@router.get("/user/jobs/{job_id}/similar")
def similar_postings(
    job_id: int,
    first: int | None = Query(None),
    after: str | None = Query(None),
    user: AuthedUser = Depends(require_admin),
) -> SimilarPostings:
    """The rows of the person's ranking nearest to one posting, less its near
    copies, nearest first."""
    limit = _first(first)
    if not db.query_one(
        "SELECT 1 FROM jobs j JOIN posting_embeddings pe ON pe.url = j.url WHERE j.id = %s",
        (job_id,),
    ):
        raise refuse(404, "NOT_FOUND", "no embedded posting with that id")
    query = {"q": "similar", "job": job_id}
    after_distance, after_id = decode_cursor(after, query) if after else (None, None)
    rows = db.query(
        for_you.SIMILAR,
        {
            "user_id": user.id,
            "job_id": job_id,
            "near_copy": recommendations.NEAR_COPY,
            "after_distance": after_distance,
            "after_id": after_id,
            "limit": limit + 1,
        },
    )
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1] if rows else None
    return SimilarPostings(
        nodes=[
            SimilarPosting(
                job_id=r["id"],
                url=r["url"],
                company=r["company"],
                title=r["title"],
                locations=r["locations"] or [],
                posted=r["posted"],
                pay=_pay(r),
                similarity=1 - float(r["distance"]),
            )
            for r in rows
        ],
        page_info=PageInfo(
            end_cursor=cursor_for(query, [float(last["distance"]), last["id"]]) if last else None,
            has_next_page=more,
        ),
        total_count=rows[0]["total"] if rows else 0,
    )
