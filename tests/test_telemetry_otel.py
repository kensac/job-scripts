"""Logs and traces go where the errors go, and every error names its trace."""

from __future__ import annotations

import logging

import pytest

from api import telemetry, worker
from api.tasks import runtime


class _Fake:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []
        self.exceptions: list[tuple[BaseException, str, dict]] = []

    def capture(self, distinct_id, event, properties):
        self.events.append((event, distinct_id, properties))

    def capture_exception(self, exc, distinct_id, properties):
        self.exceptions.append((exc, distinct_id, properties))


@pytest.fixture
def traced(monkeypatch):
    """A real in-process tracer with an in-memory exporter, beside a fake
    PostHog client, so spans and their ids can be read back."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client = _Fake()
    monkeypatch.setattr(telemetry, "_client", client)
    monkeypatch.setattr(telemetry, "_tracer", trace.get_tracer("test", tracer_provider=provider))
    return client, exporter


def test_without_a_key_init_is_a_no_op_that_says_so(monkeypatch, caplog):
    monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
    monkeypatch.setattr(telemetry, "_client", None)
    with caplog.at_level(logging.WARNING, logger="jobtracker_telemetry"):
        telemetry.init("jobtracker-worker", "test-worker")
    assert telemetry._client is None and telemetry._tracer is None
    assert any("DISABLED" in r.message for r in caplog.records)
    with telemetry.task_span({"id": 1, "kind": "k", "payload": {}}, "w") as ids:
        assert ids == {}
    assert telemetry.trace_context() == {}


@pytest.mark.asyncio
async def test_a_task_runs_inside_a_span_and_its_failure_names_the_trace(traced, monkeypatch):
    client, exporter = traced

    async def bad(task_id, payload):
        raise ValueError("boom")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", bad)
    tid = runtime.enqueue("test_kind", {"source": "acme"})
    await worker.run_once()

    (span,) = exporter.get_finished_spans()
    assert span.name == "task test_kind"
    assert span.attributes["task.id"] == tid and span.attributes["task.source"] == "acme"
    assert span.attributes["worker"] == worker.WORKER_NAME
    assert span.status.status_code.name == "ERROR"
    trace_id = format(span.get_span_context().trace_id, "032x")
    ((_, _, props),) = client.exceptions
    assert props["trace_id"] == trace_id and props["span_id"]
    assert props["instance"] == telemetry.INSTANCE
    assert client.events[0][2]["trace_id"] == trace_id


def test_an_event_inside_a_span_carries_the_trace_and_outside_carries_none(traced):
    client, _ = traced
    telemetry.capture("outside")
    assert "trace_id" not in client.events[0][2]
    with telemetry.task_span({"id": 9, "kind": "k", "payload": {"user_id": 3}}, "w") as ids:
        telemetry.capture("inside")
    assert ids["trace_id"] and client.events[1][2]["trace_id"] == ids["trace_id"]
