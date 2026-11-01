from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware

from api import ai_pause, db, metrics, telemetry
from api.ai import access as ai_access
from api.auth import require_user
from api.board import visibility
from api.models import Ok
from api.problem import PUBLIC_REFUSALS, REFUSALS, Problem, ProblemResponse
from api.routers import (
    admin,
    analytics,
    application,
    apply,
    companies,
    filter_insights,
    filters,
    gmail,
    jobs,
    mail,
    public_job_lists,
    requirements,
    resolve,
    saved_views,
    source_admin,
    sources,
    spend,
    stats,
    task_models,
    users,
)

# uvicorn configures its own loggers and leaves the root at WARNING, so the
# service's INFO records, the telemetry startup line among them, were dropped
# before any handler saw them; the api looked uninstrumented in its own logs
# while shipping. Same call the worker makes at startup.
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import core.store  # noqa: F401  (creates ai_queries on import)

    db.init_schema()
    # Every person's board membership is recomputed once per process start,
    # so a roll never serves an empty board for longer than a worker's poll.
    for u in db.query("SELECT id FROM users"):
        visibility.request_refresh(u["id"])
    metrics.serve()
    telemetry.init("jobtracker-api")
    _warm_admin_pages()
    yield
    telemetry.shutdown()


def _warm_admin_pages() -> None:
    """The admin pages whose default view takes 15 to 70 s to compute, and the
    jobs list's verdict tabs, start computing now, in the background, so the first visit after a roll does not
    wait for them (api.stale_cache)."""
    from api.routers.admin import (
        health,
        ledger_stats,
        posting_inspection,
        query_ledger,
        review_gates,
    )

    ledger_stats._stats.warm()
    query_ledger._options.warm()
    posting_inspection._checked_urls.warm()
    for verdict in posting_inspection._VERDICT_HOLDS:
        posting_inspection._verdict_totals.warm(
            verdict, lambda verdict=verdict: posting_inspection._verdict_total(verdict)
        )
    health._content_mix.warm()
    # Both panels sit on the spend page and read its default window. A report
    # warmed for any other window leaves the page's own request to wait the
    # full computation after every roll.
    days = 30
    spend._spend_pages.warm(days, lambda: spend._spend(days))
    default_report = (days, None, None, None, None)
    screens = json.dumps(db.get_config("title_screens"), sort_keys=True)
    review_gates._reports.warm(
        (*default_report, screens), lambda: review_gates._report(*default_report)
    )


app = FastAPI(
    title="jobtracker-api",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)
# Compressed at the origin because the leg that pays for size is the one from
# this host in Nuremberg to the Cloudflare edge beside Vercel in Virginia,
# which Cloudflare's own compression does not cover. /v1/sources is 1.1 MB of
# JSON and 165 KB gzipped; over that distance 1.1 MB took 710 ms more than an
# empty reply and 150 KB took 340 ms (2026-11-01). A stored PDF is already
# compressed and streams with its Content-Length, which gzip would drop.
app.add_middleware(
    GZipMiddleware, exclude_content_types=(*DEFAULT_EXCLUDED_CONTENT_TYPES, "application/pdf")
)


@app.exception_handler(ai_pause.AIPaused)
async def _ai_paused(_request: Request, _exc: ai_pause.AIPaused) -> JSONResponse:
    """A route that reaches a model without `require_config` (an admin
    check, a re-check a report asks for) answers the same refusal."""
    return JSONResponse(
        status_code=503,
        content={"detail": {"code": "AI_PAUSED", "message": ai_access.PAUSED_MESSAGE}},
    )


@app.exception_handler(Exception)
async def _unhandled(_request: Request, _exc: Exception) -> JSONResponse:
    """An unhandled error answers in the problem shape every refusal has, so
    a client reads `detail.code` here too instead of special-casing a
    plain-text body. The message says nothing of the exception: its text can
    carry SQL, paths or user data. Starlette re-raises after this answer, so
    the server still logs the traceback and `_capture_unhandled` records it."""
    problem = Problem(code="INTERNAL_ERROR", message="the server failed to answer this request")
    return JSONResponse(status_code=500, content=ProblemResponse(detail=problem).model_dump())


@app.middleware("http")
async def _capture_unhandled(request: Request, call_next):
    """An unhandled exception in a request handler is recorded with the
    request it failed on and the caller it failed for, then re-raised so
    `_unhandled` answers it. HTTPException is a handled answer, not a
    failure, and never reaches here."""
    try:
        return await call_next(request)
    except Exception as exc:
        telemetry.capture_exception(
            exc,
            distinct_id=request.headers.get("X-User-Sub") or telemetry.SERVICE,
            properties={"path": request.url.path, "method": request.method},
        )
        raise


app.include_router(users.router, prefix="/v1", responses=REFUSALS)
app.include_router(public_job_lists.router, prefix="/v1", responses=PUBLIC_REFUSALS)
app.include_router(
    saved_views.router, prefix="/v1", dependencies=[Depends(require_user)], responses=REFUSALS
)
app.include_router(jobs.router, prefix="/v1", responses=REFUSALS)
app.include_router(application.router, prefix="/v1", responses=REFUSALS)
app.include_router(apply.router, prefix="/v1", responses=REFUSALS)
app.include_router(filters.router, prefix="/v1", responses=REFUSALS)
app.include_router(sources.router, prefix="/v1", responses=REFUSALS)
app.include_router(stats.router, prefix="/v1", responses=REFUSALS)
app.include_router(requirements.router, prefix="/v1", responses=REFUSALS)
app.include_router(admin.router, prefix="/v1", responses=REFUSALS)
app.include_router(source_admin.router, prefix="/v1", responses=REFUSALS)
app.include_router(task_models.router, prefix="/v1", responses=REFUSALS)
app.include_router(analytics.router, prefix="/v1", responses=REFUSALS)
app.include_router(companies.router, prefix="/v1", responses=REFUSALS)
app.include_router(spend.router, prefix="/v1", responses=REFUSALS)
app.include_router(mail.router, prefix="/v1", responses=REFUSALS)
app.include_router(resolve.router, prefix="/v1", responses=REFUSALS)
app.include_router(filter_insights.router, prefix="/v1", responses=REFUSALS)
app.include_router(gmail.router, prefix="/v1", responses=REFUSALS)
metrics.instrument(app)
# At import, before the middleware stack is built: the instrumentation takes a
# lazy tracer that starts producing spans once telemetry.init() sets the
# provider at startup, and stays a no-op without a key.
telemetry.instrument_app(app)


@app.get("/healthz")
def healthz() -> Ok:
    db.query_one("SELECT 1 AS ok")
    return Ok()


@app.get("/v1/openapi")
def openapi_schema(user=Depends(require_user)) -> dict[str, Any]:
    """The schema itself, so a client can generate against the API it is
    talking to rather than a file someone remembered to copy.

    `dict[str, Any]` IS the shape here, not a gap in one. Modelling the
    OpenAPI document in pydantic, to describe the route that returns the
    OpenAPI document, is a circle rather than a contract. So this declares an
    open object on purpose, and is not an exception to the rule that every
    operation declares what it returns."""
    return app.openapi()
