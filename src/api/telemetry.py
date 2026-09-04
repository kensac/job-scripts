"""Errors, events, logs and traces to PostHog, one project end to end.

The division of labour is deliberate. A traceback goes here, where it is
grouped and searchable; a CONDITION (a source failing for a day, a purpose
failing whole, a host blocking) stays in api/health.py, whose detectors know
what the numbers mean. Nothing here decides anything; it records, in one
place, so that a failure inside the service that never becomes a bad HTTP
answer - a worker crash, a retry, a fetch or model call failing mid-pipeline
- is visible somewhere at all.

Four signals, one client key, one host:

- exceptions and events through the PostHog SDK (`capture_exception`,
  `capture`);
- logs: every record the service logs, through OpenTelemetry to
  `{host}/i/v1/logs`, carrying the trace it was logged inside;
- traces: one span per HTTP request (FastAPI instrumentation), one per
  worker task (`task_span`), and one per outbound `requests` call (the
  board and ATS fetches), through OpenTelemetry to `{host}/i/v1/traces`;
- the trace and span ids on every exception and event, so an error links to
  the request or task it happened inside.

Off entirely without POSTHOG_API_KEY: every function is a no-op, so tests
and a laptop checkout run without a network destination. Never raises: a
telemetry failure must not become the second failure of the thing it was
recording.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
from collections.abc import Iterator
from typing import Any

from api import metrics

logger = logging.getLogger("jobtracker_telemetry")

SERVICE = "service"
_client: Any = None
_tracer: Any = None
_logger_provider: Any = None
_tracer_provider: Any = None
HOST = socket.gethostname()
# The process's fleet name once init() has run (a worker's WORKER_NAME, the
# api's hostname); what tells hetzner-worker-2 from oci in every record.
INSTANCE = HOST
# The commit the image was built from (deploy/Dockerfile sets it from the
# build arg), so a regression is dated to a release rather than to a day.
RELEASE = os.environ.get("JOBTRACKER_REVISION", "unknown")
DEFAULT_HOST = "https://us.i.posthog.com"


def init(service: str = "jobtracker", instance: str | None = None) -> None:
    """Builds the clients once, from POSTHOG_API_KEY and POSTHOG_HOST. Called
    at API startup and worker startup with the process's service name and,
    for a worker, its fleet name (hetzner-worker-2, oci, ...), so the api and
    each worker are distinguishable in every trace and log line as
    service.name and service.instance.id. Safe to call again."""
    global _client, _tracer, _logger_provider, _tracer_provider
    if _client is not None:
        return
    key = os.environ.get("POSTHOG_API_KEY")
    # Said once at startup either way. A layer that never raises and is a
    # no-op without a key has two silent modes that look identical from
    # PostHog's side; the log line is what tells "not configured" from
    # "configured and broken", and it is the line a diagnosis starts from.
    if not key:
        logger.warning(f"telemetry: DISABLED (POSTHOG_API_KEY unset); release={RELEASE}")
        return
    host = os.environ.get("POSTHOG_HOST", DEFAULT_HOST).rstrip("/")
    from opentelemetry import trace
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from posthog import Posthog

    _client = Posthog(
        project_api_key=key,
        host=host,
        # Long-running processes: the default batching thread is right, and
        # shutdown() flushes what it holds.
        enable_exception_autocapture=False,
    )
    headers = {"Authorization": f"Bearer {key}"}
    global INSTANCE
    INSTANCE = instance or HOST
    resource = Resource.create(
        {
            "service.name": service,
            "service.instance.id": INSTANCE,
            "service.version": RELEASE,
            "host.name": HOST,
            "deployment.environment": os.environ.get("JOBTRACKER_ENV", "production"),
        }
    )
    # How much goes. Errors and events always go; traces and logs are the
    # volume, and the right amount is a measurement, not a guess: ship all of
    # it first, read the daily volume in PostHog, then turn these two down if
    # the bill or the noise says so. Both are env because they are read once
    # at startup, before the database is open.
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    ratio = float(os.environ.get("POSTHOG_TRACE_SAMPLE", "1.0"))
    log_level = logging.getLevelName(os.environ.get("POSTHOG_LOG_LEVEL", "INFO").upper())
    # PostHog is a generic OTLP receiver over HTTP; the full /i/v1/* paths are
    # required, the base-endpoint convention is not honoured.
    _tracer_provider = TracerProvider(
        resource=resource, sampler=ParentBased(TraceIdRatioBased(ratio))
    )
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{host}/i/v1/traces", headers=headers))
    )
    trace.set_tracer_provider(_tracer_provider)
    _tracer = trace.get_tracer(service)
    # Outbound HTTP: the board pulls and ATS resolvers go through `requests`,
    # so every board fetch is a span with its status, under the task's span.
    RequestsInstrumentor().instrument()
    # Database time inside the same spans. psycopg connections are opened
    # by the pool after this point, so patching the module is enough; the
    # statement text is not recorded, only the operation and its duration.
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    PsycopgInstrumentor().instrument()

    _logger_provider = LoggerProvider(resource=resource)
    _logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{host}/i/v1/logs", headers=headers))
    )
    set_logger_provider(_logger_provider)
    # Every record the service logs, at INFO and above, with the active trace
    # attached by the SDK. The root logger, so a module needs no registration.
    handler = LoggingHandler(level=log_level, logger_provider=_logger_provider)
    logging.getLogger().addHandler(handler)
    logger.info(
        f"telemetry: enabled service={service} host={host} release={RELEASE} "
        f"process_host={HOST} traces_sampled={ratio} logs_from={logging.getLevelName(log_level)}"
    )


def instrument_app(app: Any) -> None:
    """One span per HTTP request, named by route, with status and the
    caller's subject. Called at import, before the middleware stack is built
    (Starlette refuses middleware after the first request). The tracer it
    takes is the global proxy, which produces nothing until init() sets a
    provider and stays a no-op without a key."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="healthz,metrics",
        http_capture_headers_server_request=["X-User-Sub"],
    )


def shutdown() -> None:
    for name, thing in (
        ("client", _client),
        ("tracer", _tracer_provider),
        ("logs", _logger_provider),
    ):
        if thing is None:
            continue
        try:
            thing.shutdown()
        except Exception:
            logger.exception(f"telemetry: {name} shutdown failed")


def trace_context() -> dict[str, str]:
    """The active trace and span ids, so an exception or event links to the
    request or task it happened inside. Empty outside any span."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return {}
        return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}
    except Exception:
        return {}


@contextlib.contextmanager
def task_span(task: dict[str, Any], worker: str) -> Iterator[dict[str, str]]:
    """A span around one worker task, with what a trace of the queue needs to
    read: the task, its kind, the worker, the attempt. Yields the span's ids,
    which stay readable after the span has ended, so the worker's failure
    branch (which runs after the span closed) can still name the trace the
    failure belongs to. An empty dict, and no span, without a key. The
    exception, if any, is recorded on the span and re-raised."""
    ids: dict[str, str] = {}
    if _tracer is None:
        yield ids
        return
    payload = task.get("payload") or {}
    with _tracer.start_as_current_span(f"task {task['kind']}") as span:
        span.set_attribute("task.id", task["id"])
        span.set_attribute("task.kind", task["kind"])
        span.set_attribute("task.attempts", task.get("attempts") or 0)
        span.set_attribute("worker", worker)
        if payload.get("source"):
            span.set_attribute("task.source", payload["source"])
        if payload.get("user_id") is not None:
            span.set_attribute("task.user_id", payload["user_id"])
        ids.update(trace_context())
        yield ids


def _props(properties: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "host": HOST,
        "instance": INSTANCE,
        "release": RELEASE,
        **trace_context(),
        **(properties or {}),
    }


def capture(
    event: str, distinct_id: str = SERVICE, properties: dict[str, Any] | None = None
) -> None:
    """A queryable failure event. Never raises: a telemetry failure must not
    become the second failure of the thing it was recording."""
    if _client is None:
        return
    try:
        _client.capture(distinct_id=distinct_id, event=event, properties=_props(properties))
    except Exception:
        metrics.TELEMETRY_FAILURES.labels("event").inc()
        logger.exception(f"telemetry: capture({event}) failed")


def capture_exception(
    exc: BaseException, distinct_id: str = SERVICE, properties: dict[str, Any] | None = None
) -> None:
    if _client is None:
        return
    try:
        _client.capture_exception(exc, distinct_id=distinct_id, properties=_props(properties))
    except Exception:
        metrics.TELEMETRY_FAILURES.labels("exception").inc()
        logger.exception("telemetry: capture_exception failed")
