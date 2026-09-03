from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from api import db, metrics
from api.auth import require_user
from api.routers import (
    admin,
    analytics,
    companies,
    filter_insights,
    filters,
    gmail,
    jobs,
    mail,
    requirements,
    sources,
    spend,
    stats,
    task_models,
    users,
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import core.store  # noqa: F401  (creates ai_queries on import)

    db.init_schema()
    metrics.serve()
    yield


app = FastAPI(
    title="jobtracker-api",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)

app.include_router(users.router, prefix="/v1")
app.include_router(jobs.router, prefix="/v1")
app.include_router(filters.router, prefix="/v1")
app.include_router(sources.router, prefix="/v1")
app.include_router(stats.router, prefix="/v1")
app.include_router(requirements.router, prefix="/v1")
app.include_router(admin.router, prefix="/v1")
app.include_router(task_models.router, prefix="/v1")
app.include_router(analytics.router, prefix="/v1")
app.include_router(companies.router, prefix="/v1")
app.include_router(spend.router, prefix="/v1")
app.include_router(mail.router, prefix="/v1")
app.include_router(filter_insights.router, prefix="/v1")
app.include_router(filter_insights.user_router, prefix="/v1")
app.include_router(gmail.router, prefix="/v1")
metrics.instrument(app)


@app.get("/healthz")
def healthz():
    db.query_one("SELECT 1 AS ok")
    return {"ok": True}


@app.get("/v1/openapi")
def openapi_schema(user=Depends(require_user)):
    return app.openapi()
