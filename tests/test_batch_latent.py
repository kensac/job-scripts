"""Two bugs that had not fired, and the stall a guard makes silent.

Both were found by measurement and neither had produced a wrong number yet.
They are here because what kept each one harmless was a property of the
provider - batches being small, or a collection never being retried - rather
than anything in this code.
"""

from __future__ import annotations

from api import db
from api.tasks.runtime import _batch_event_hook


def _park(kind: str, hours: float = 0.5) -> int:
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status, started_at) "
        "VALUES (%s, '{}'::jsonb, 'awaiting_batch', now() - make_interval(secs => %s)) "
        "RETURNING id",
        (kind, int(hours * 3600)),
    )
    assert row is not None
    return row["id"]


def _batch(
    purpose: str, requests: int, failed: int, hours: float = 1.0, model: str = "gpt-5-mini"
) -> None:
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, purpose, model, requests, completed, "
        "failed_count, status, submitted_at) VALUES (%s, %s, %s, %s, %s, %s, 'completed', "
        "now() - make_interval(secs => %s))",
        (
            f"b-{purpose}-{model}-{requests}-{failed}-{hours}",
            purpose,
            model,
            requests,
            requests - failed,
            failed,
            int(hours * 3600),
        ),
    )


def _completed(kind: str, minutes: float, n: int = 1) -> None:
    for _ in range(n):
        db.execute(
            "INSERT INTO tasks (kind, payload, status, started_at, finished_at) "
            "VALUES (%s, '{}'::jsonb, 'done', now() - make_interval(secs => %s), now())",
            (kind, int(minutes * 60)),
        )


def _running(kind: str, minutes: float, worker: str = "oci") -> int:
    row = db.query_one(
        "INSERT INTO tasks (kind, payload, status, started_at, last_heartbeat, worker, attempts) "
        "VALUES (%s, '{}'::jsonb, 'running', now() - make_interval(secs => %s), now(), %s, 1) "
        "RETURNING id",
        (kind, int(minutes * 60), worker),
    )
    assert row is not None
    return row["id"]


class TestVerifyNewDoesNotOverlapItself:
    """verify_new batches and parks, and its predicate - jobs with no verdict -
    stays true while the batch is in flight. A second task re-selects the same
    jobs and pays again. Its three siblings all guard against this; it did not.
    """

    def _schedule(self):
        from api.worker import schedule_ingest_cycle

        schedule_ingest_cycle()

    def _verify_tasks(self):
        return db.query("SELECT id, status FROM tasks WHERE kind = 'verify_new' ORDER BY id")

    def test_a_cycle_enqueues_it_when_nothing_is_in_flight(self, f):
        self._schedule()
        assert len(self._verify_tasks()) == 1

    def test_a_cycle_does_not_enqueue_it_while_one_is_parked(self, f):
        parked = _park("verify_new")
        self._schedule()
        assert [t["id"] for t in self._verify_tasks()] == [parked]

    def test_a_cycle_does_not_enqueue_it_while_one_is_running(self, f):
        db.execute(
            "INSERT INTO tasks (kind, payload, status) VALUES ('verify_new', '{}'::jsonb, 'running')"
        )
        self._schedule()
        assert len(self._verify_tasks()) == 1


class TestUsageIsRecordedOncePerBatch:
    """The hook reports a batch's TOTALS every time it is collected, and the
    write was additive - so a second collection doubled both the batch row and
    the spend ledger. Reachable by a task that collects, fails, is requeued,
    and reattaches to the same batch.
    """

    def _collect(self, batch_id="b1", inp=1000, out=100):
        hook = _batch_event_hook(1, "comp", "gpt-5-nano")
        hook(batch_id, "submitted", {"requests": 1, "completed": 0, "failed": 0})
        hook(batch_id, "completed", {"input_tokens": inp, "output_tokens": out})

    def _batch(self, batch_id="b1"):
        return db.query_one(
            "SELECT input_tokens, output_tokens, est_cost_usd FROM ai_batches "
            "WHERE provider_batch_id = %s",
            (batch_id,),
        )

    def _ledger(self):
        return db.query("SELECT * FROM api_usage WHERE user_id IS NULL AND purpose = 'comp'")

    def test_one_collection_records_the_totals(self):
        self._collect()
        row = self._batch()
        assert row is not None
        assert (row["input_tokens"], row["output_tokens"]) == (1000, 100)
        assert len(self._ledger()) == 1

    def test_collecting_the_same_batch_again_does_not_double_it(self):
        self._collect()
        self._collect()
        row = self._batch()
        assert row is not None
        assert (row["input_tokens"], row["output_tokens"]) == (1000, 100)

    def test_collecting_again_does_not_add_a_second_ledger_row(self):
        """The worse half: /admin/spend reads api_usage, so a duplicate row is
        money reported that was never spent."""
        self._collect()
        self._collect()
        assert len(self._ledger()) == 1

    def test_two_different_batches_are_both_recorded(self):
        """The fix must not deduplicate across batches - each is its own spend."""
        self._collect("b1")
        self._collect("b2")
        assert len(self._ledger()) == 2


class TestAStalledSweepIsVisible:
    """Every batched sweep now refuses to start while one of its own is in
    flight. That stops double payment and makes a stuck task a SILENT stall -
    the sweep simply never runs again. This is what says so.
    """

    def _alerts(self):
        from api import health

        return [f for f in health.detect() if f["kind"] == "batch_parked_too_long"]

    def test_a_task_parked_within_the_window_is_not_an_alert(self, f):
        """Inside the window, waiting is what a batch is supposed to do."""
        _park("extract_comp", hours=1)
        assert self._alerts() == []

    def test_a_task_parked_past_the_window_is_critical(self, f):
        from core.batch import completion_window_seconds

        _park("extract_comp", hours=completion_window_seconds() / 3600 + 2)
        alerts = self._alerts()
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"
        assert alerts[0]["subject"] == "extract_comp"

    def test_the_message_says_the_sweep_is_not_running(self, f):
        """The consequence, not just the symptom - 'parked' means nothing to
        someone who does not know a guard stops the next one starting."""
        from core.batch import completion_window_seconds

        _park("verify_new", hours=completion_window_seconds() / 3600 + 2)
        assert "not running at all" in self._alerts()[0]["message"]

    def test_the_threshold_is_the_providers_own_window(self, f, monkeypatch):
        """Derived, not picked: inside the window the provider has promised
        nothing yet, and past it both it and poll_batches have failed."""
        import core.batch

        _park("extract_comp", hours=7)
        assert self._alerts() == []
        monkeypatch.setattr(core.batch, "completion_window_seconds", lambda: 6 * 3600)
        assert len(self._alerts()) == 1

    def test_each_kind_is_its_own_alert(self, f):
        from core.batch import completion_window_seconds

        over = completion_window_seconds() / 3600 + 2
        _park("extract_comp", hours=over)
        _park("classify_mail", hours=over)
        assert {a["subject"] for a in self._alerts()} == {"extract_comp", "classify_mail"}


class TestABatchThatFailedEveryRequestIsVisible:
    """A 499-request mail_classify batch failed every request on 2026-09-02 and
    its task closed 'done' with no error, because collection succeeded at
    collecting nothing. Zero tokens and zero cost are correct - nothing ran -
    so the spend ledger cannot see it either. Nothing reported it at all.
    """

    def _alerts(self):
        from api import health

        return [f for f in health.detect() if f["kind"] == "batch_failed_whole"]

    def test_a_batch_that_failed_every_request_is_critical(self, f):
        _batch("mail_classify", requests=499, failed=499)
        alerts = self._alerts()
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"
        assert alerts[0]["subject"] == "mail_classify"

    def test_a_batch_that_succeeded_is_not_an_alert(self, f):
        _batch("mail_classify", requests=499, failed=0)
        assert self._alerts() == []

    def test_a_partial_failure_is_a_different_event_and_does_not_alert(self, f):
        """A batch fails WHOLE when the submission is rejected. Some requests
        failing means bad inputs, which is not certainly a defect - and giving
        it a failure-rate threshold would be inventing one."""
        _batch("mail_classify", requests=499, failed=498)
        assert self._alerts() == []

    def test_an_empty_batch_is_not_a_whole_failure(self, f):
        """0 = 0 satisfies failed_count = requests arithmetically and is not a
        failure of anything."""
        _batch("mail_classify", requests=0, failed=0)
        assert self._alerts() == []

    def test_it_stops_alerting_once_the_work_can_no_longer_be_resubmitted(self, f):
        """Bounded to one completion window, like the parked detector: inside
        it the batch can still be resubmitted, so the alert is actionable."""
        from core.batch import completion_window_seconds

        _batch(
            "mail_classify", requests=499, failed=499, hours=completion_window_seconds() / 3600 + 2
        )
        assert self._alerts() == []

    def test_the_message_says_why_nothing_else_reports_it(self, f):
        _batch("extract_comp", requests=10, failed=10)
        message = self._alerts()[0]["message"]
        assert "done" in message and "no cost" in message

    def test_each_model_is_its_own_alert(self, f):
        """One model rejecting a parameter is the case this exists for, so a
        second model failing must not be folded into the first."""
        _batch("extract_comp", requests=10, failed=10, model="gpt-5-mini")
        _batch("extract_comp", requests=10, failed=10, model="gpt-5.6-luna")
        assert {a["detail"]["model"] for a in self._alerts()} == {"gpt-5-mini", "gpt-5.6-luna"}


class TestAWedgedHandlerIsVisible:
    """Liveness is a daemon thread beating on a timer, so it proves the process
    exists and says nothing about whether the work advances. The reaper keys on
    that heartbeat, so a wedged handler is never requeued - it holds its worker
    until someone recreates the container.

    On 2026-09-03 a match_mail task sat 60 minutes against a 5.2-minute worst
    case, on attempt 1, heartbeat 20 seconds old, holding one of three workers
    while ten poll_batches queued behind it. Every earlier run of that kind had
    been requeued once or twice; this one never was, because the change that
    stopped the spurious reaping also removed the only recovery for a real one.
    """

    def _alerts(self):
        from api import health

        return [f for f in health.detect() if f["kind"] == "handler_overdue"]

    def test_a_task_inside_its_kinds_worst_case_is_not_an_alert(self, f):
        _completed("match_mail", minutes=5, n=18)
        _running("match_mail", minutes=4)
        assert self._alerts() == []

    def test_a_task_past_its_worst_case_but_inside_the_grace_is_not_an_alert(self, f):
        """The reaper's own timeout is the margin. A new worst case by a minute
        is a slow run, not a wedged one."""
        from api.tasks.runtime import HEARTBEAT_TIMEOUT_MINUTES

        _completed("match_mail", minutes=5, n=18)
        _running("match_mail", minutes=5 + HEARTBEAT_TIMEOUT_MINUTES - 1)
        assert self._alerts() == []

    def test_a_task_past_its_worst_case_by_more_than_the_grace_is_critical(self, f):
        _completed("match_mail", minutes=5, n=18)
        task_id = _running("match_mail", minutes=60)
        alerts = self._alerts()
        assert len(alerts) == 1
        assert alerts[0]["severity"] == "critical"
        assert alerts[0]["subject"] == "match_mail"
        assert alerts[0]["detail"]["task_id"] == task_id

    def test_the_threshold_is_per_kind_not_per_fleet(self, f):
        """A long kind and a short kind running the same wall-clock time are
        not the same event. ingest_source at 60 minutes is normal; match_mail
        at 60 minutes is wedged."""
        _completed("match_mail", minutes=5, n=18)
        _completed("ingest_source", minutes=188, n=50)
        _running("match_mail", minutes=60)
        _running("ingest_source", minutes=60)
        assert [a["subject"] for a in self._alerts()] == ["match_mail"]

    def test_a_kind_with_no_completed_history_does_not_alert(self, f):
        """No history is no bound. Alerting on a handler that has never
        finished would fire on every kind's first ever run."""
        _running("match_mail", minutes=600)
        assert self._alerts() == []

    def test_the_message_says_the_reaper_will_not_recover_it(self, f):
        """The consequence, not the symptom. 'Running a long time' reads as
        slow; the point is that nothing will requeue it."""
        _completed("match_mail", minutes=5, n=18)
        _running("match_mail", minutes=60)
        assert "will not requeue it" in self._alerts()[0]["message"]

    def test_the_sample_behind_the_bound_is_reported(self, f):
        """The bound IS the sample: a worst case over three runs is much weaker
        than one over a thousand, and the reader can see which they have."""
        _completed("match_mail", minutes=5, n=3)
        _running("match_mail", minutes=60)
        assert self._alerts()[0]["detail"]["completed_runs"] == 3

    def test_a_fresh_heartbeat_does_not_suppress_it(self, f):
        """The whole point: the heartbeat is a timer and stays fresh forever,
        which is why the reaper misses this and why this detector cannot key
        on the heartbeat either."""
        _completed("match_mail", minutes=5, n=18)
        task_id = _running("match_mail", minutes=60)
        db.execute("UPDATE tasks SET last_heartbeat = now() WHERE id = %s", (task_id,))
        assert len(self._alerts()) == 1


class TestAlertSubjectKind:
    """An alert's subject is not one kind of thing: two detectors put a source
    in it, one a host, one a provider and user, one a task kind. The dashboard
    linked all of them to the sources page, which is right for two of five.
    """

    def test_each_detector_declares_what_its_subject_is(self):
        from api import health

        assert health.subject_kind_for("ats_text_collapse") == health.SUBJECT_SOURCE
        assert health.subject_kind_for("extraction_failing") == health.SUBJECT_HOST
        assert health.subject_kind_for("oauth_token_invalid") == health.SUBJECT_PROVIDER_USER
        assert health.subject_kind_for("batch_parked_too_long") == health.SUBJECT_TASK
        assert health.subject_kind_for("batch_failed_whole") == health.SUBJECT_PURPOSE
        assert health.subject_kind_for("handler_overdue") == health.SUBJECT_TASK

    def test_a_purpose_is_not_a_task_kind(self):
        """batch_failed_whole's subject is the spend ledger's purpose
        ("mail_classify"); the task kind is "classify_mail". Declaring it
        SUBJECT_TASK would link to a task kind that does not exist."""
        from api import health

        assert health.SUBJECT_PURPOSE != health.SUBJECT_TASK

    def test_generated_rate_spike_kinds_are_matched_by_shape(self):
        """They are built per check_type, so listing them would mean a new
        check_type silently losing its link."""
        from api import health

        assert health.subject_kind_for("closed_rate_spike") == health.SUBJECT_SOURCE
        assert health.subject_kind_for("clearance_rate_spike") == health.SUBJECT_SOURCE

    def test_an_unknown_kind_gets_no_subject_kind(self):
        """None rather than a default: a wrong link is worse than no link,
        which is the whole reason this exists."""
        from api import health

        assert health.subject_kind_for("something_added_later") is None

    def test_every_detector_this_module_emits_is_mapped(self):
        """A detector without an entry renders as plain text - honest, but it
        should be a decision rather than an oversight."""
        import inspect
        import re

        from api import health

        emitted = set(re.findall(r'"kind": "([a-z_]+)"', inspect.getsource(health)))
        unmapped = {k for k in emitted if health.subject_kind_for(k) is None}
        assert not unmapped, f"detectors with no subject_kind: {unmapped}"

    def test_the_admin_endpoint_annotates_open_alerts(self, client, admin_headers):
        from api import health

        health.record(
            [
                {
                    "kind": "batch_parked_too_long",
                    "subject": "extract_comp",
                    "severity": "critical",
                    "message": "m",
                    "detail": {},
                }
            ]
        )
        body = client.get("/v1/admin/health", headers=admin_headers).json()
        assert body["open"][0]["subject_kind"] == health.SUBJECT_TASK


class TestPollBatchesDoesNotPileUp:
    """A poll is idempotent and stateless - it reports on whatever is open
    right now - so a second one waiting behind the first has nothing of its
    own to do. Eleven had queued behind an hour of scraping, and each would
    hold a worker slot when it ran, competing with the collection of batches
    already paid for.
    """

    def _schedule(self):
        from api.worker import schedule_ingest_cycle

        schedule_ingest_cycle()

    def _polls(self):
        return db.query("SELECT id, status FROM tasks WHERE kind = 'poll_batches' ORDER BY id")

    def test_one_is_enqueued_when_none_is_waiting(self, f):
        self._schedule()
        assert len(self._polls()) == 1

    def test_a_second_is_not_enqueued_from_a_later_bucket(self, f):
        """Across buckets is the case that matters. The dedupe key already
        stops two polls in the same bucket, so a test that calls the scheduler
        twice in one minute passes without the guard - which is what my first
        version of this test did."""
        db.execute(
            "INSERT INTO tasks (kind, payload, status, dedupe_key) "
            "VALUES ('poll_batches', '{}'::jsonb, 'pending', 'pollbatch:an-earlier-bucket')"
        )
        self._schedule()
        assert len(self._polls()) == 1

    def test_a_second_is_not_enqueued_while_one_is_running(self, f):
        db.execute(
            "INSERT INTO tasks (kind, payload, status) "
            "VALUES ('poll_batches', '{}'::jsonb, 'running')"
        )
        self._schedule()
        assert len(self._polls()) == 1

    def test_a_finished_poll_does_not_block_the_next(self, f):
        """The guard must not stop polling altogether - a parked task waits on
        it, and batches expire."""
        db.execute(
            "INSERT INTO tasks (kind, payload, status, finished_at) "
            "VALUES ('poll_batches', '{}'::jsonb, 'done', now())"
        )
        self._schedule()
        assert len(self._polls()) == 2
