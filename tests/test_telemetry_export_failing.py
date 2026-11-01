"""A worker whose OTLP exporter is failing is an alert, not only a counter on
/metrics that no rule reads."""

from __future__ import annotations

import pytest

from api import db, health, telemetry, worker


@pytest.fixture
def clean():
    db.execute("DELETE FROM worker_status WHERE name LIKE 'test-%'")
    yield
    db.execute("DELETE FROM worker_status WHERE name LIKE 'test-%'")


def _worker(name: str, *, started: str, ok: str | None, failed: str | None, seen: str = "now()"):
    """Times are SQL expressions on the database clock, which is the clock
    the detector reads."""
    db.execute(
        f"INSERT INTO worker_status (name, started_at, last_seen, telemetry_ok_at, "
        f"telemetry_failed_at) VALUES (%s, {started}, {seen}, {ok or 'NULL'}, {failed or 'NULL'})",
        (name,),
    )


def _failing():
    return {f["subject"]: f for f in health.detect() if f["kind"] == "telemetry_export_failing"}


AGO_10M = "now() - interval '10 minutes'"
AGO_1H = "now() - interval '1 hour'"
AGO_2H = "now() - interval '2 hours'"


def test_only_a_live_worker_failing_now_with_nothing_delivered_alerts(clean):
    _worker("test-failing", started=AGO_2H, ok=AGO_10M, failed="now()")
    _worker("test-never-delivered", started=AGO_10M, ok=None, failed="now()")
    # One failed batch between good ones.
    _worker("test-blip", started=AGO_2H, ok="now()", failed="now() - interval '5 seconds'")
    # Failing, but started too recently to have delivered anything yet.
    _worker("test-just-started", started="now()", ok=None, failed="now()")
    # An old failure and nothing since to send.
    _worker("test-idle", started=AGO_2H, ok=AGO_2H, failed=AGO_1H)
    _worker("test-healthy", started=AGO_2H, ok="now()", failed=None)
    # No POSTHOG_API_KEY: nothing is exported, nothing is judged.
    _worker("test-disabled", started=AGO_2H, ok=None, failed=None)
    _worker("test-dead", started=AGO_2H, ok=AGO_2H, failed=AGO_1H, seen=AGO_1H)
    found = _failing()
    assert set(found) == {"test-failing", "test-never-delivered"}
    assert found["test-failing"]["severity"] == "warning"
    assert 9 < found["test-failing"]["detail"]["minutes"] < 11
    assert found["test-never-delivered"]["detail"]["last_ok_at"] is None
    assert health.subject_kind_for("telemetry_export_failing") == health.SUBJECT_WORKER


def test_the_heartbeat_reports_the_exporters_last_outcomes(monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setattr(telemetry, "EXPORT_OK_AT", None)
    monkeypatch.setattr(telemetry, "EXPORT_FAILED_AT", None)
    exporter = telemetry._counted(InMemorySpanExporter(), "traces")
    exporter.export([object()])
    ok_at = telemetry.EXPORT_OK_AT
    assert ok_at is not None and telemetry.EXPORT_FAILED_AT is None
    exporter.shutdown()
    exporter.export([object()])
    failed_at = telemetry.EXPORT_FAILED_AT
    assert failed_at is not None and telemetry.EXPORT_OK_AT is ok_at

    worker._report_worker_status()
    row = db.query_one(
        "SELECT telemetry_ok_at, telemetry_failed_at FROM worker_status WHERE name = %s",
        (worker.WORKER_NAME,),
    )
    assert row == {"telemetry_ok_at": ok_at, "telemetry_failed_at": failed_at}
