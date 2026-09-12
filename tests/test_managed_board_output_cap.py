"""A managed board run must not cap the model's output.

Every truncated custom verdict landed on exactly 120 completion tokens with
zero variance: p50, p95 and max all 120 across 1,852 failures, against a p95
of 313 and a max of 1,050 for verdicts that succeeded. The cap was the whole
of the failure.

The same prompt, model and hash run through the user-filter path, which sets
no cap, failed 0 times in 2,075. Through the managed-board path it failed
1,725 times in 11,629.

A truncated verdict is recorded as `failed`, and a board with fail_closed
drops it silently, so this does not surface as an error. It surfaces as a
board that is quietly missing a seventh of its postings.
"""

from __future__ import annotations

import inspect


def test_the_managed_board_run_sets_no_output_cap():
    from tasks import managed_boards

    source = inspect.getsource(managed_boards.handle_run_managed_board)
    assert "max_output_tokens" not in source, (
        "the managed board run is capping model output again; the user-filter "
        "path sets no cap and this one must not either"
    )


def test_the_reservation_constant_is_not_a_cap():
    """It is named `RESERVATION` because it sizes the budget hold, not the
    model. Using it as `max_output_tokens` is what caused the truncation, so
    the two must not be the same number by accident either."""
    from api import managed_board_runs as runs

    # 320 is the measured p95 of a successful verdict. A reservation smaller
    # than that under-holds budget for one run in twenty.
    assert runs.FILTER_OUTPUT_RESERVATION_TOKENS >= 320
