"""Failures inside the service leave a record somewhere other than a log
line inside a container that the next roll replaces."""

from __future__ import annotations

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
def fake(monkeypatch):
    client = _Fake()
    monkeypatch.setattr(telemetry, "_client", client)
    return client


def test_without_a_client_everything_is_a_no_op(monkeypatch):
    monkeypatch.setattr(telemetry, "_client", None)
    telemetry.capture("anything", properties={"x": 1})
    telemetry.capture_exception(RuntimeError("x"))
    telemetry.shutdown()


def test_every_record_carries_host_and_release(fake):
    telemetry.capture("thing", properties={"k": "v"})
    (event, distinct_id, props) = fake.events[0]
    assert (event, distinct_id, props["k"]) == ("thing", telemetry.SERVICE, "v")
    assert props["host"] == telemetry.HOST and props["release"] == telemetry.RELEASE


def test_a_telemetry_failure_never_becomes_the_second_failure(monkeypatch):
    class _Broken:
        def capture(self, **kw):
            raise RuntimeError("posthog down")

        def capture_exception(self, *a, **kw):
            raise RuntimeError("posthog down")

    monkeypatch.setattr(telemetry, "_client", _Broken())
    telemetry.capture("thing")
    telemetry.capture_exception(ValueError("original"))


@pytest.mark.asyncio
async def test_a_failed_task_records_the_traceback_and_a_queryable_event(fake, monkeypatch):
    async def bad_handler(task_id, payload):
        raise ValueError("boom")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", bad_handler)
    tid = runtime.enqueue("test_kind", {"user_id": 7, "source": "acme"})
    await worker.run_once()

    ((exc, distinct_id, props),) = fake.exceptions
    assert isinstance(exc, ValueError) and distinct_id == telemetry.SERVICE
    assert props["task_id"] == tid and props["task_kind"] == "test_kind"
    assert props["error_class"] == "ValueError" and props["user_id"] == 7
    assert props["source"] == "acme" and props["worker"] == worker.WORKER_NAME
    assert [e[0] for e in fake.events] == ["task_failed"]


@pytest.mark.asyncio
async def test_a_transient_failure_is_a_requeue_event_not_an_exception(fake, monkeypatch):
    async def oom(task_id, payload):
        raise RuntimeError("can't start new thread")

    monkeypatch.setitem(worker.HANDLERS, "test_kind", oom)
    runtime.enqueue("test_kind", {})
    await worker.run_once()
    assert fake.exceptions == []
    assert [e[0] for e in fake.events] == ["task_requeued"]


def test_an_unhandled_request_error_names_the_caller_and_the_route(fake, client, user_headers):
    from api.app import app

    @app.get("/v1/__boom")
    def boom():
        raise RuntimeError("handler broke")

    try:
        with pytest.raises(RuntimeError):
            client.get("/v1/__boom", headers=user_headers)
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != "/v1/__boom"
        ]
    ((exc, distinct_id, props),) = fake.exceptions
    assert str(exc) == "handler broke"
    assert distinct_id == user_headers["X-User-Sub"]
    assert (props["path"], props["method"]) == ("/v1/__boom", "GET")


def test_an_alert_opening_and_resolving_are_events(fake):
    from api import db, health

    found = [
        {
            "kind": "test_kind",
            "subject": "test-subject",
            "severity": "warning",
            "message": "m",
            "detail": {},
        }
    ]
    health.record(found)
    assert [(e[0], e[2]["kind"], e[2]["subject"]) for e in fake.events] == [
        ("alert_opened", "test_kind", "test-subject")
    ]
    # Re-firing refreshes the row and is not a second opening.
    health.record(found)
    assert len(fake.events) == 1
    db.execute(
        "UPDATE health_alerts SET last_seen = now() - interval '4 hours' "
        "WHERE kind = 'test_kind' AND subject = 'test-subject'"
    )
    health.record([])
    assert (
        fake.events[-1][0] == "alert_resolved" and fake.events[-1][2]["subject"] == "test-subject"
    )
