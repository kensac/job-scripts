"""A batch result receipt's response lives in object storage: written there
by the checkpoint, inline only while an older image runs, and moved by
tasks.receipt_responses."""

from __future__ import annotations

import asyncio

from api import data_level, db
from api.ai import batch_results
from core.batch import BatchResult
from tasks import receipt_responses
from tests.factories import make_task

USAGE = {"input_tokens": 120, "output_tokens": 7, "total_tokens": 127}


def _result(custom_id: str) -> BatchResult:
    return BatchResult(
        custom_id, text="answer", usage=USAGE, finish_reason="stop", batch_id="provider-batch"
    )


def _row(custom_id: str) -> dict:
    row = db.query_one("SELECT * FROM batch_result_receipts WHERE custom_id = %s", (custom_id,))
    assert row is not None
    return row


def _older_worker() -> None:
    db.execute(
        "INSERT INTO worker_status (name, release, data_level, data_level_release, last_seen) "
        "VALUES ('old', 'r1', %s, 'r1', now())",
        (data_level.LEVEL - 1,),
    )


def _run() -> None:
    task_id = make_task(receipt_responses.KIND, status="running")
    asyncio.run(receipt_responses.handle_move_receipt_responses(task_id, {}))


def test_a_checkpoint_writes_the_response_by_reference(f):
    task_id = f.make_task("extract_comp", {}, status="running")
    batch_results.checkpoint(task_id, [_result("a"), _result("b")], [])

    row = _row("a")
    assert row["response"] is None and row["response_object"] is not None
    assert (row["finish_reason"], row["output_tokens"]) == ("stop", 7)
    assert _row("b")["response_object"] == row["response_object"]
    [first, second] = batch_results.unconsumed(task_id)
    assert (first.text, first.usage, first.finish_reason) == ("answer", USAGE, "stop")
    assert second.custom_id == "b"


def test_while_an_older_image_runs_the_response_is_inline_and_moved_later(f):
    _older_worker()
    task_id = f.make_task("extract_comp", {}, status="running")
    batch_results.checkpoint(task_id, [_result("a")], [])
    row = _row("a")
    assert row["response"]["text"] == "answer" and row["response_object"] is None

    _run()
    assert _row("a")["response"] is not None, "the move waits for the older image"

    db.execute("UPDATE worker_status SET data_level = %s WHERE name = 'old'", (data_level.LEVEL,))
    _run()
    row = _row("a")
    assert row["response"] is None and row["response_object"] is not None
    assert (row["finish_reason"], row["output_tokens"]) == ("stop", 7)
    assert batch_results.unconsumed(task_id)[0].text == "answer"
    assert receipt_responses.inline_count() == 0


def test_switching_the_move_off_leaves_responses_inline(f, set_config):
    _older_worker()
    task_id = f.make_task("extract_comp", {}, status="running")
    batch_results.checkpoint(task_id, [_result("a")], [])
    db.execute("DELETE FROM worker_status")
    set_config("receipt_response_move_enabled", False)
    _run()
    assert _row("a")["response"] is not None
    set_config("receipt_response_move_enabled", True)
    _run()
    assert _row("a")["response"] is None


def test_a_consumed_receipt_keeps_its_outcome_when_moved(f):
    _older_worker()
    task_id = f.make_task("extract_comp", {}, status="running")
    result = _result("a")
    batch_results.checkpoint(task_id, [result], [])
    with batch_results.consume_result(task_id, result) as receipt:
        receipt.outcome = "written"
    db.execute("DELETE FROM worker_status")
    _run()
    row = _row("a")
    assert (row["outcome"], row["consumed_at"] is not None) == ("written", True)
    assert batch_results.outcome_counts(task_id) == {"written": 1}
