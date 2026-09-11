from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request

from api import db, metrics, telemetry
from api.auth import require_user
from api.board import visibility
from api.models import Ok
from api.problem import REFUSALS
from api.routers import (
    admin,
    analytics,
    application,
    apply,
    companies,
    experiments,
    filter_insights,
    filters,
    gmail,
    jobs,
    mail,
    requirements,
    resolve,
    source_admin,
    sources,
    spend,
    stats,
    task_models,
    users,
    views,
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
    yield
    telemetry.shutdown()


app = FastAPI(
    title="jobtracker-api",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)


@app.middleware("http")
async def _capture_unhandled(request: Request, call_next):
    """An unhandled exception in a request handler is recorded with the
    request it failed on and the caller it failed for, then re-raised so the
    response is the 500 it always was. HTTPException is a handled answer, not
    a failure, and never reaches here."""
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
app.include_router(
    views.router, prefix="/v1", dependencies=[Depends(require_user)], responses=REFUSALS
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
app.include_router(experiments.router, prefix="/v1", responses=REFUSALS)
app.include_router(task_models.router, prefix="/v1", responses=REFUSALS)
app.include_router(analytics.router, prefix="/v1", responses=REFUSALS)
app.include_router(companies.router, prefix="/v1", responses=REFUSALS)
app.include_router(spend.router, prefix="/v1", responses=REFUSALS)
app.include_router(mail.router, prefix="/v1", responses=REFUSALS)
app.include_router(resolve.router, prefix="/v1", responses=REFUSALS)
app.include_router(filter_insights.router, prefix="/v1", responses=REFUSALS)
app.include_router(filter_insights.user_router, prefix="/v1", responses=REFUSALS)
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
