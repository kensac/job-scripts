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
