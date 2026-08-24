from __future__ import annotations

import os
import time

from fastapi import FastAPI, Request
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from api import db

HTTP_REQUESTS = Counter(
    "jobtracker_http_requests_total",
    "HTTP requests",
    ["method", "path", "status"],
)
HTTP_DURATION = Histogram(
    "jobtracker_http_request_duration_seconds",
    "HTTP request duration",
    ["method", "path"],
)
TASKS_PROCESSED = Counter(
    "jobtracker_worker_tasks_total",
    "Worker tasks processed",
    ["kind", "status"],
)
AI_TOKENS = Counter(
    "jobtracker_ai_tokens_total",
    "AI tokens spent",
    ["key_source", "purpose"],
)
TASK_QUEUE_DEPTH = Gauge(
    "jobtracker_task_queue_depth",
    "Pending tasks",
)
OLDEST_PENDING_AGE = Gauge(
    "jobtracker_oldest_pending_task_age_seconds",
    "Age of the oldest pending task (SLO signal: growing = fleet behind)",
)
TASK_DURATION = Histogram(
    "jobtracker_task_duration_seconds",
    "Task wall time by kind",
    ["kind"],
    buckets=(1, 5, 15, 60, 300, 900, 3600, 7200),
)
AI_CALLS = Counter(
    "jobtracker_ai_calls_total",
    "AI API calls",
    ["provider", "model", "outcome"],  # ok | error | rate_limited
)
AI_CALL_DURATION = Histogram(
    "jobtracker_ai_call_duration_seconds",
    "AI call latency",
    ["provider"],
    buckets=(0.5, 1, 2, 4, 8, 15, 30, 60, 120),
)
SCRAPES = Counter(
    "jobtracker_scrapes_total",
    "Page scrapes",
    ["outcome"],  # ok | empty
)
SCRAPE_DURATION = Histogram(
    "jobtracker_scrape_duration_seconds",
    "Scrape wall time",
    buckets=(1, 3, 5, 10, 20, 40, 60, 120),
)
CHECKS = Counter(
    "jobtracker_checks_total",
    "Fresh AI verdicts recorded",
    ["check_type", "outcome"],  # passed | rejected | failed
)
CACHED_VERDICTS = Counter(
    "jobtracker_cached_verdicts_total",
    "Checks skipped because a cached verdict existed",
)
WORKER_CONCURRENCY = Gauge(
    "jobtracker_worker_concurrency",
    "Current adaptive in-flight limit of this worker",
)
REAPER_REQUEUES = Counter(
    "jobtracker_reaper_requeues_total",
    "Tasks requeued after their worker died",
)
INGEST_JOBS = Counter(
    "jobtracker_ingest_jobs_total",
    "Ingest pipeline stages",
    ["source", "stage"],  # fetched | upserted | checked
)
AI_COST_USD = Counter(
    "jobtracker_ai_cost_usd_total",
    "Estimated AI spend in USD (app-side pricing tables)",
    ["provider", "model", "key_source"],
)
BOARD_ROWS = Counter(
    "jobtracker_board_rows_total",
    "Automatic board row changes",
    ["action"],  # materialized | demoted
)


def _queue_depth() -> float:
    row = db.query_one("SELECT COUNT(*) AS c FROM tasks WHERE status = 'pending'")
    return float(row["c"]) if row else 0.0


def _oldest_pending_age() -> float:
    row = db.query_one(
        "SELECT COALESCE(EXTRACT(EPOCH FROM now() - MIN(created_at)), 0) AS age "
        "FROM tasks WHERE status = 'pending'"
    )
    return float(row["age"]) if row else 0.0


def serve() -> None:
    """Expose the default registry on an internal-only port (not via traefik)."""
    TASK_QUEUE_DEPTH.set_function(_queue_depth)
    OLDEST_PENDING_AGE.set_function(_oldest_pending_age)
    start_http_server(int(os.environ.get("JOBTRACKER_METRICS_PORT", "9091")))


def instrument(app: FastAPI) -> None:
    @app.middleware("http")
    async def _metrics_middleware(request: Request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        HTTP_REQUESTS.labels(request.method, path, str(response.status_code)).inc()
        HTTP_DURATION.labels(request.method, path).observe(time.monotonic() - start)
        return response
