"""A detector that fails is one alert, not a silent hour.

health.detect() wraps its sections so a raising detector opens detector_failed
for itself. That wrapping covered four sections and not the three that ran
inline ahead of the loop, so a failure in those still took the whole run down
and every open alert auto-resolved with nothing left to re-observe it. Both
tests here fail against that shape.
"""

from __future__ import annotations

import json

import pytest

from api import db, health


def _raise_on_nth(monkeypatch, n: int) -> None:
    """Make the nth db.query inside detect() fail, the way a bad plan does."""
    orig = db.query
    seen = {"i": 0}

    def flaky(sql, *args, **kwargs):
        seen["i"] += 1
        if seen["i"] == n:
            raise RuntimeError("simulated detector failure")
        return orig(sql, *args, **kwargs)

    monkeypatch.setattr(db, "query", flaky)


def test_a_failing_inline_detector_does_not_take_the_run_down(monkeypatch):
    """The first query in detect() belongs to the per-source detectors, which
    used to run before the protective loop. A failure there must be one
    finding, not an exception out of detect()."""
    _raise_on_nth(monkeypatch, 1)

    found = health.detect()

    failed = [f for f in found if f["kind"] == "detector_failed"]
    assert failed, "a raising detector must report itself"
    assert "_detect_sources" in {f["subject"] for f in failed}
    assert failed[0]["severity"] == "critical"


def test_the_other_detectors_still_run_when_one_fails(monkeypatch):
    """The point of the isolation: one failure costs one detector's findings,
    not the hour's."""
    everything = {f["kind"] for f in health.detect()}
    _raise_on_nth(monkeypatch, 1)

    after = health.detect()

    assert {f["kind"] for f in after} - {"detector_failed"} == everything - {"detector_failed"}, (
        "sections after the failing one must still report"
    )


def test_a_failing_detector_does_not_resolve_alerts_it_cannot_evaluate(monkeypatch):
    db.execute(
        "INSERT INTO health_alerts (kind, subject, severity, message, last_seen) "
        "VALUES ('source_feed_empty', 'board', 'critical', 'm', now() - interval '1 day')"
    )

    def boom():
        raise RuntimeError("board query failed")

    monkeypatch.setattr(health, "_detect_boards", boom)
    run = health.detect()
    health.record(run)

    assert run.failed_detectors == ["_detect_boards"]
    assert (
        db.query_one("SELECT resolved_at FROM health_alerts WHERE kind = 'source_feed_empty'")[
            "resolved_at"
        ]
        is None
    )


def test_a_recovered_detector_can_resolve_its_old_alert(monkeypatch):
    db.execute(
        "INSERT INTO health_alerts (kind, subject, severity, message, last_seen) "
        "VALUES ('source_feed_empty', 'board', 'critical', 'm', now() - interval '1 day')"
    )
    monkeypatch.setattr(health, "_detect_boards", list)

    health.record(health.detect())

    assert (
        db.query_one("SELECT resolved_at FROM health_alerts WHERE kind = 'source_feed_empty'")[
            "resolved_at"
        ]
        is not None
    )


def test_an_alert_with_unknown_detector_ownership_never_false_clears():
    db.execute(
        "INSERT INTO health_alerts (kind, subject, severity, message, last_seen) "
        "VALUES ('new_detector_kind', 'subject', 'warning', 'm', now() - interval '1 day')"
    )

    health.record(health.detect())

    assert (
        db.query_one("SELECT resolved_at FROM health_alerts WHERE kind = 'new_detector_kind'")[
            "resolved_at"
        ]
        is None
    )


def test_a_task_whose_progress_is_not_a_number_does_not_break_a_detector(f):
    """`(progress->>'total')::int` in a WHERE clause is not safe: Postgres does
    not promise to filter before it casts, so one row like this failed the
    whole query and the detector reported detector_failed every hour.

    The corpus writes exactly this shape, which is how it was found.
    """
    db.execute(
        "INSERT INTO tasks (kind, status, payload, progress, finished_at) "
        "VALUES (%s, 'done', %s, %s, now())",
        (
            "run_filter_batch_chunk",
            json.dumps({}),
            json.dumps({"done": "not-a-number", "total": "nor-this"}),
        ),
    )

    found = health.detect()

    assert not [
        x for x in found if x["kind"] == "detector_failed" and x["subject"] == "_detect_silent"
    ], "a non-numeric progress value must drop out of the comparison, not raise"


def test_a_non_numeric_fetch_failure_count_does_not_break_board_detection(f):
    db.execute(
        "INSERT INTO tasks (kind, status, payload, progress, worker, finished_at) "
        "VALUES ('ingest_source', 'done', '{}', %s, 'worker', now())",
        (json.dumps({"cached": 20, "fetch_failed": "unknown"}),),
    )

    health._detect_boards()


@pytest.mark.parametrize("column,key", [("progress", "total"), ("progress", "done")])
def test_the_guarded_read_yields_null_for_a_non_number(column, key):
    """The helper the detectors share, checked directly against Postgres."""
    sql = health._int_from(column, key)
    row = db.query_one(
        f"SELECT {sql} AS v FROM (SELECT %s::jsonb AS {column}) t",
        (json.dumps({key: "abc"}),),
    )
    assert row is not None and row["v"] is None

    row = db.query_one(
        f"SELECT {sql} AS v FROM (SELECT %s::jsonb AS {column}) t",
        (json.dumps({key: 7}),),
    )
    assert row is not None and row["v"] == 7
