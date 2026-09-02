"""A DB restart is infrastructure moving, not a task failing.

homelab restarted Postgres twice to swap in pgvector. Both in-flight parked
tasks came back "terminating connection due to administrator command", each
severance was booked as a real attempt, and two restarts exhausted the cap.
The tasks permanently failed - which stripped their batch_ids, while the
OpenAI batches they named went on to COMPLETE. Paid results with nothing left
to consume them.

The classifier matched substrings and that phrase was not among them. These
tests pin the classification by TYPE, so the next transient database error
does not need someone to have predicted its wording.
"""

from __future__ import annotations

import psycopg
import pytest

from api import worker


@pytest.mark.parametrize(
    "exc",
    [
        psycopg.errors.AdminShutdown("terminating connection due to administrator command"),
        psycopg.errors.CannotConnectNow("the database system is starting up"),
        psycopg.errors.ConnectionException("connection lost"),
        psycopg.OperationalError("consuming input failed: EOF detected"),
        psycopg.InterfaceError("the connection is closed"),
    ],
)
def test_infrastructure_loss_is_transient(exc):
    assert worker._is_transient(exc) is True


def test_the_exact_restart_message_that_was_missed():
    """The specific production case, kept as its own test so a refactor that
    drops AdminShutdown from the tuple fails with a message naming the
    incident rather than a generic parametrised id."""
    exc = psycopg.errors.AdminShutdown("terminating connection due to administrator command")
    assert worker._is_transient(exc), "a pg restart must requeue, not fail the task"


@pytest.mark.parametrize(
    "message",
    [
        "can't start new thread",
        "Cannot allocate memory",
        "out of memory",
        "no space left on device",
    ],
)
def test_host_resource_exhaustion_is_still_transient(message):
    """These arrive as OSError/RuntimeError with no distinguishing type, so
    they remain string-matched. Narrowing the string list to only these must
    not have dropped them."""
    assert worker._is_transient(OSError(message)) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("unparsable model output"),
        KeyError("missing payload field"),
        psycopg.errors.UndefinedColumn('column "nope" does not exist'),
        psycopg.errors.UniqueViolation("duplicate key"),
    ],
)
def test_real_bugs_are_not_transient(exc):
    """The failure this must not develop: treating a genuine defect as
    transient means retrying it MAX_ATTEMPTS times and then failing anyway,
    with the real error buried under two identical retries. Note that
    UndefinedColumn and UniqueViolation are psycopg errors but NOT
    OperationalError subclasses, which is what makes the type test precise
    rather than 'anything from the driver'."""
    assert worker._is_transient(exc) is False
