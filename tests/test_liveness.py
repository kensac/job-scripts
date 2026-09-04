"""The things that go wrong at 3am.

The other files check data shapes. This one checks that the moving parts are
still moving: nothing wedged, nothing parked forever, and the two independent
spellings of the visibility predicate still agree.

The wedged/parked checks are monitors of a running fleet, so they stay on real
data. The predicate comparison is a test of code, and now runs on every pull
request against the corpus - which materialises its boards with the
application's own write-time predicate, so the comparison stays able to fail.

Deliberately not asserted here, because they are known backlogs rather than
invariants and would fail today: 14,196 verdicts whose url matches no job (a
pre-normalisation artefact) and 10,342 active jobs still awaiting a first
closed verdict (verify_new is working through them). Asserting a number that
is legitimately non-zero would train us to ignore the file.
"""

from __future__ import annotations

import pytest

from api import db


def _count(sql: str, params=None) -> int:
    row = db.query_one(sql, params)
    assert row is not None
    return int(next(iter(row.values())))


@pytest.mark.integration
def test_no_task_is_wedged_in_running():
    """The reaper requeues a task whose worker died after HEARTBEAT_TIMEOUT.
    Anything still 'running' a day later means the reaper itself is not
    working, which is invisible until the queue backs up.

    Real data only: this is a monitor of the reaper, not a test of any code path.
    Nothing generates tasks in the corpus, so there is no reaper for it to be watching.
    """
    wedged = db.query(
        "SELECT id, kind, worker, started_at FROM tasks "
        "WHERE status = 'running' AND started_at < now() - interval '24 hours' LIMIT 5"
    )
    assert not wedged, f"tasks stuck running for over a day: {wedged}"


@pytest.mark.integration
def test_no_batch_is_parked_past_the_providers_own_window():
    """Tasks now park on provider batches instead of holding a worker. The
    failure mode that replaces 'worker blocked' is 'parked forever', so this
    is the check that the poller is actually resuming them.

    The window is the provider's, read from the same constant the submitter
    uses, so this cannot drift from what we asked for.


    Real data only: a monitor of the batch poller, for the same reason.
    """
    from core.batch import completion_window_seconds

    window = completion_window_seconds()
    stale = db.query(
        "SELECT provider_batch_id, status, submitted_at FROM ai_batches "
        "WHERE status NOT IN ('completed', 'failed', 'expired', 'cancelled') "
        "AND submitted_at < now() - make_interval(secs => %s) LIMIT 5",
        (window * 2,),
    )
    assert not stale, f"batches unresolved past twice the completion window: {stale}"


@pytest.mark.integration
def test_no_task_parked_without_batches_to_wait_for():
    """awaiting_batch with an empty batch_ids list would wait forever - the
    poller has nothing to check, so nothing ever resumes it.

    Real data only: a monitor of the submitter. The corpus draws task status and
    payload independently, so it parks tasks with no batch_ids and would fail on its
    own generator.
    """
    orphaned = db.query(
        "SELECT id, kind FROM tasks WHERE status = 'awaiting_batch' "
        "AND COALESCE(jsonb_array_length(payload->'batch_ids'), 0) = 0 LIMIT 5"
    )
    assert not orphaned, f"parked with nothing to wait on: {orphaned}"


@pytest.mark.corpus
def test_the_two_visibility_predicates_agree():
    """The board is defined twice - as a read-time predicate in routers/jobs.py
    and as a write-time predicate in tasks/board.py. They have already drifted
    once (zero-enabled-filters behaved differently in each), and the drift is
    invisible until a user's board is wrong.

    This runs both against real data and compares. It is the single most
    valuable thing in this file, because no fixture reproduces the
    combinations real data contains.
    """
    from api import criteria
    from api.routers.jobs import _VISIBILITY

    user = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
    if user is None:
        pytest.skip("no users in the synced copy")
    uid = user["id"]
    settings = db.query_one("SELECT * FROM user_settings WHERE user_id = %s", (uid,))
    params = {"uid": uid, "bypass_sponsorship": False, **criteria.params(settings)}

    read_side = db.query_one(
        _VISIBILITY.format(columns="COUNT(*) AS c", extra="", criteria=criteria.SQL), params
    )
    assert read_side is not None

    # Every row the write path materialised must be visible to the read path.
    # The reverse is not required: the read path also shows jobs a user has
    # touched, which materialisation never created.
    materialised_but_hidden = db.query_one(
        _VISIBILITY.format(
            columns="COUNT(*) AS c",
            extra="",
            criteria=criteria.SQL,
        ).replace(
            "FROM jobs j",
            "FROM jobs j JOIN user_jobs m ON m.job_id = j.id AND m.user_id = %(uid)s",
        ),
        params,
    )
    assert materialised_but_hidden is not None
    board_rows = _count("SELECT count(*) FROM user_jobs WHERE user_id = %s", (uid,))
    assert materialised_but_hidden["c"] == board_rows, (
        f"{board_rows - materialised_but_hidden['c']} materialised board rows are "
        "invisible to the read-time predicate - the two spellings have drifted"
    )


@pytest.mark.corpus
def test_health_detectors_execute_against_real_data():
    """detect() is ratio comparisons over live tables with sample floors. It
    has silently stopped being evaluable before (a denominator polluted by rows
    no fixture produced), so running it against real volume is the only way to
    know it still works."""
    from api import health

    alerts = health.detect()
    assert isinstance(alerts, list)
    for a in alerts:
        assert {"kind", "subject", "severity", "message"} <= set(a), f"malformed alert: {a}"


@pytest.mark.integration
def test_every_batch_row_has_a_task_that_exists():
    """
    Real data only: ai_batches.task_id has no foreign key, so in production this can
    genuinely orphan. The corpus draws it from the tasks it has already written, so it
    never can.
    """
    orphans = _count(
        "SELECT count(*) FROM ai_batches b LEFT JOIN tasks t ON t.id = b.task_id "
        "WHERE b.task_id IS NOT NULL AND t.id IS NULL"
    )
    assert orphans == 0


@pytest.mark.integration
def test_token_counts_are_never_negative():
    """
    Real data only: the subject is what the providers reported. The corpus samples
    token counts from a measured range whose floor is zero.
    """
    bad = _count(
        "SELECT count(*) FROM ai_queries "
        "WHERE prompt_tokens < 0 OR completion_tokens < 0 OR total_tokens < 0"
    )
    assert bad == 0


@pytest.mark.integration
def test_every_enabled_filter_has_a_prompt_hash():
    """Verdicts are keyed on prompt_hash. A filter without one can never match
    a verdict, so its user's board silently loses every job.

    Real data only: the corpus computes a prompt_hash for every filter it writes, so a
    missing one cannot occur.
    """
    missing = _count(
        "SELECT count(*) FROM user_filters WHERE enabled "
        "AND (prompt_hash IS NULL OR prompt_hash = '')"
    )
    assert missing == 0
