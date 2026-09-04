"""A live worker on a different release from the api, past a roll's
duration, is a host that did not deploy. gcp-vps ran two rolls behind for
an hour on 2026-09-04 and every other signal said it was fine."""

from __future__ import annotations

import datetime

import pytest

from api import db, health, telemetry


@pytest.fixture
def clean():
    db.execute("DELETE FROM worker_status WHERE name LIKE 'test-%'")
    yield
    db.execute("DELETE FROM worker_status WHERE name LIKE 'test-%'")


def _worker(name: str, release: str | None, started_minutes_ago: int, fresh: bool = True):
    started = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=started_minutes_ago)
    seen = "now()" if fresh else "now() - interval '1 hour'"
    db.execute(
        f"INSERT INTO worker_status (name, started_at, last_seen, release) "
        f"VALUES (%s, %s, {seen}, %s)",
        (name, started, release),
    )


def _mixed():
    return {f["subject"]: f for f in health.detect() if f["kind"] == "fleet_mixed_release"}


def test_only_a_fresh_worker_on_another_release_past_the_roll_window_alerts(clean, monkeypatch):
    monkeypatch.setattr(telemetry, "RELEASE", "abc1234")
    _worker("test-same", "abc1234", 120)
    _worker("test-behind", "0ld0000", 120)
    _worker("test-rolling", "0ld0000", 5)
    _worker("test-dead", "0ld0000", 120, fresh=False)
    _worker("test-unreported", None, 120)
    found = _mixed()
    assert set(found) == {"test-behind", "test-unreported"}
    assert found["test-behind"]["detail"] == {
        "worker": "test-behind",
        "worker_release": "0ld0000",
        "api_release": "abc1234",
        "minutes": found["test-behind"]["detail"]["minutes"],
    }
    assert (
        "0ld0000" in found["test-behind"]["message"]
        and "abc1234" in found["test-behind"]["message"]
    )
    assert health.subject_kind_for("fleet_mixed_release") == health.SUBJECT_WORKER


def test_a_local_build_compares_nothing(clean, monkeypatch):
    monkeypatch.setattr(telemetry, "RELEASE", "unknown")
    _worker("test-behind", "0ld0000", 120)
    assert _mixed() == {}
