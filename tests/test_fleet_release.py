"""A live worker on a different release from most of the fleet, past a roll's
duration, is a host whose pin is not following. gcp-vps ran two rolls behind
for an hour on 2026-09-04 and every other signal said it was fine."""

from __future__ import annotations

import datetime

import pytest

from api import db, health, telemetry


@pytest.fixture
def clean():
    db.execute("DELETE FROM worker_status WHERE name LIKE 'test-%'")
    yield
    db.execute("DELETE FROM worker_status WHERE name LIKE 'test-%'")


# A worker started exactly fleet_roll_minutes ago sits on the boundary the
# detector tests, and the two sides of that comparison come from different
# clocks: the fixture builds started_at from Python's, and the detector reads
# Postgres's now(), which is TRANSACTION START time. The margin is whatever
# elapsed between the insert and the detect() call, milliseconds, less the skew
# between the two clocks - measured at -6 ms here, the same order. This suite
# failed once in a full run and has passed alone every time since, which is the
# shape a margin that small produces.
#
# Restoring 120 below does NOT reproduce the failure on demand; it passed six
# runs for six. So this is the arithmetic that fits, not a proven cause. The
# margin costs nothing and removes the sensitivity either way.
PAST_ROLL = 600
INSIDE_ROLL = 5


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


def test_only_a_fresh_worker_off_the_fleet_release_past_the_roll_window_alerts(clean):
    _worker("test-a", "abc1234", PAST_ROLL)
    _worker("test-b", "abc1234", PAST_ROLL)
    _worker("test-behind", "0ld0000", PAST_ROLL)
    _worker("test-rolling", "0ld0000", INSIDE_ROLL)
    _worker("test-dead", "0ld0000", PAST_ROLL, fresh=False)
    _worker("test-unreported", None, PAST_ROLL)
    found = _mixed()
    assert set(found) == {"test-behind", "test-unreported"}
    assert found["test-behind"]["detail"] == {
        "worker": "test-behind",
        "worker_release": "0ld0000",
        "fleet_release": "abc1234",
        "minutes": found["test-behind"]["detail"]["minutes"],
    }
    assert (
        "0ld0000" in found["test-behind"]["message"]
        and "abc1234" in found["test-behind"]["message"]
    )
    assert health.subject_kind_for("fleet_mixed_release") == health.SUBJECT_WORKER


def test_the_worker_running_the_check_is_not_the_reference(clean, monkeypatch):
    """data_health runs on whichever worker claims it. On 2026-11-21 one a
    bump behind flagged hetzner, a bump ahead, as a host that did not deploy."""
    monkeypatch.setattr(telemetry, "RELEASE", "0ld0000")
    _worker("test-a", "abc1234", PAST_ROLL)
    _worker("test-b", "abc1234", PAST_ROLL)
    assert _mixed() == {}


def test_a_local_build_is_not_compared(clean):
    _worker("test-a", "abc1234", PAST_ROLL)
    _worker("test-local", "unknown", PAST_ROLL)
    assert _mixed() == {}
