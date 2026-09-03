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
