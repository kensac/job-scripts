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


def _queue_depth() -> float:
    row = db.query_one("SELECT COUNT(*) AS c FROM tasks WHERE status = 'pending'")
    return float(row["c"]) if row else 0.0


def serve() -> None:
    """Expose the default registry on an internal-only port (not via traefik)."""
    TASK_QUEUE_DEPTH.set_function(_queue_depth)
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
