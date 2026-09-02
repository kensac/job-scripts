"""What a batched sweep asked, and a sample of what came back.

Batched extraction records tokens, cost and a model, and nothing about the
instructions behind them. These tests pin the two properties that make the
record worth having: it is written for every caller at one seam rather than
per handler, and it changes no resolution key - a prompt edit must not
invalidate the catalog the way it deliberately invalidates a filter's verdicts.
"""

from __future__ import annotations

import pytest

from api import db
from api.tasks import runtime
from core.prompts import PROMPT_SAMPLE_SIZE, prompt_hash


class _Res:
    def __init__(self, text=None, error=None):
        self.text, self.error = text, error


def _results(n: int, prefix: str = "u"):
    return {f"{prefix}{i}": _Res(text='{"ok": true}') for i in range(n)}


def _prompts():
    return db.query("SELECT * FROM ai_prompts ORDER BY id")


def _samples(prompt_id: int):
    return db.query(
        "SELECT * FROM ai_prompt_samples WHERE prompt_id = %s ORDER BY id", (prompt_id,)
    )


class TestPromptIdentity:
    def test_the_text_is_stored_once_however_many_sweeps_send_it(self):
        """21 distinct prompts sit behind 68,735 production rows. Storing the
        text per row is 75 MB; storing it per distinct text is 32 KB, which is
        what makes keeping the text affordable at all."""
        for _ in range(5):
            runtime._record_prompt("comp", "Extract the compensation.")
        rows = _prompts()
        assert len(rows) == 1
        assert rows[0]["batches"] == 5
        assert rows[0]["instructions"] == "Extract the compensation."

    def test_a_changed_prompt_is_a_new_row_not_an_overwrite(self):
        """The whole question is "what changed", which needs both texts."""
        runtime._record_prompt("comp", "Extract the compensation.")
        runtime._record_prompt("comp", "Extract the compensation, in USD.")
        rows = _prompts()
        assert len(rows) == 2
        assert {r["instructions"] for r in rows} == {
            "Extract the compensation.",
            "Extract the compensation, in USD.",
        }

    def test_whitespace_is_a_different_prompt(self):
        """Deliberately not normalised: the question is whether these are the
        same bytes we sent last time, and a normaliser is one more thing that
        can disagree with itself between versions."""
        assert prompt_hash("a b") != prompt_hash("a  b")

    def test_first_seen_is_kept_and_last_seen_moves(self):
        runtime._record_prompt("comp", "P")
        first = _prompts()[0]
        runtime._record_prompt("comp", "P")
        again = _prompts()[0]
        assert again["first_seen_at"] == first["first_seen_at"]
        assert again["last_seen_at"] >= first["last_seen_at"]

    def test_recording_never_takes_down_a_sweep(self, monkeypatch):
        """Provenance is reporting. A sweep that is otherwise ready to spend
        money correctly must not die because a log write failed."""

        def boom(*a, **k):
            raise RuntimeError("database is having a moment")

        monkeypatch.setattr(runtime.db, "query_one", boom)
        assert runtime._record_prompt("comp", "P") is None


class TestSamples:
    def test_outputs_are_sampled_against_the_prompt(self):
        pid = runtime._record_prompt("comp", "P")
        assert pid is not None
        runtime._record_prompt_samples(pid, _results(3))
        rows = _samples(pid)
        assert len(rows) == 3
        assert all(r["output"] == '{"ok": true}' for r in rows)

    def test_the_cap_is_per_prompt_version_not_per_sweep(self):
        """A prompt running hourly for a year holds 100 rows, not 8,760."""
        pid = runtime._record_prompt("comp", "P")
        assert pid is not None
        for cycle in range(3):
            runtime._record_prompt_samples(pid, _results(60, prefix=f"c{cycle}u"))
        assert len(_samples(pid)) == PROMPT_SAMPLE_SIZE

    def test_a_new_prompt_version_gets_its_own_sample_budget(self):
        """Otherwise the first prompt's samples would starve every later one,
        and the comparison the samples exist for needs both sides."""
        old = runtime._record_prompt("comp", "P1")
        new = runtime._record_prompt("comp", "P2")
        assert old is not None and new is not None
        runtime._record_prompt_samples(old, _results(PROMPT_SAMPLE_SIZE + 20))
        runtime._record_prompt_samples(new, _results(5))
        assert len(_samples(old)) == PROMPT_SAMPLE_SIZE
        assert len(_samples(new)) == 5

    def test_errors_are_sampled_too(self):
        """A prompt edit that starts producing unparseable JSON is exactly the
        change worth seeing, and it leaves no output behind."""
        pid = runtime._record_prompt("comp", "P")
        assert pid is not None
        runtime._record_prompt_samples(pid, {"u1": _Res(error="no output text")})
        rows = _samples(pid)
        assert len(rows) == 1
        assert rows[0]["output"] is None
        assert rows[0]["error"] == "no output text"

    def test_no_prompt_means_no_samples_rather_than_orphans(self):
        runtime._record_prompt_samples(None, _results(3))
        assert db.query("SELECT 1 FROM ai_prompt_samples") == []

    def test_sampling_never_takes_down_a_sweep(self, monkeypatch):
        pid = runtime._record_prompt("comp", "P")

        def boom(*a, **k):
            raise RuntimeError("database is having a moment")

        monkeypatch.setattr(runtime.db, "query_one", boom)
        runtime._record_prompt_samples(pid, _results(3))


class TestNoFork:
    def test_the_prompt_tables_are_in_no_resolution_key(self):
        """The trap this had to avoid. ai_queries keys custom verdicts on
        prompt_hash so that changing a filter's prompt makes prior verdicts
        unreachable - correct there, catastrophic if generalised: a comp or
        requirements prompt change would invalidate 49k extracted rows and
        re-pay for the catalog.
        """
        import inspect

        from api.routers import jobs
        from api.tasks import board
        from core import store

        for module in (jobs, board, store):
            source = inspect.getsource(module)
            assert "ai_prompts" not in source, module.__name__
            assert "ai_prompt_samples" not in source, module.__name__

    def test_extraction_rows_survive_a_prompt_change(self, f):
        """The behaviour, not just the absence of a reference: an extraction
        recorded under one prompt is still there after another is recorded."""
        _, url = f.make_ready_job(content="a long job description " * 20)
        f.make_requirements(url, skills_required=["Python"])
        runtime._record_prompt("requirements", "V1")
        runtime._record_prompt("requirements", "V2")
        assert db.query_one("SELECT COUNT(*) AS n FROM job_requirements")["n"] == 1
        assert db.query_one("SELECT COUNT(*) AS n FROM job_skills WHERE url = %s", (url,))["n"] == 1


class TestSeam:
    def test_run_batched_is_where_recording_happens(self):
        """Recorded at the one caller every batched handler goes through, so a
        new AI caller gets provenance without anyone wiring it - the same
        property that makes the spend ledger complete."""
        import inspect

        source = inspect.getsource(runtime.run_batched)
        assert "_record_prompt(" in source
        # Exactly one, on the single exit both paths reach. Two call sites is
        # how the reattach path ends up silently skipping what the submit path
        # records, and a requeued sweep is the harder case to notice missing.
        assert source.count("_record_prompt_samples(") == 1

    def test_the_batch_row_carries_the_prompt(self):
        pid = runtime._record_prompt("comp", "P")
        hook = runtime._batch_event_hook(1, "comp", "gpt-5-nano", prompt_id=pid)
        hook("batch_x", "submitted", {"requests": 3, "completed": 0, "failed": 0})
        row = db.query_one("SELECT prompt_id FROM ai_batches WHERE provider_batch_id = 'batch_x'")
        assert row is not None and row["prompt_id"] == pid

    @pytest.mark.parametrize("handler", ["comp", "requirements", "mail_classify"])
    def test_every_batched_handler_goes_through_the_seam(self, handler):
        import importlib
        import inspect

        module = importlib.import_module(f"api.tasks.{handler}")
        source = inspect.getsource(module)
        assert "run_batched(" in source, handler
