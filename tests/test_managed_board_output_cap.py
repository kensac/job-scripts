"""Managed board calls must allow enough output for a full verdict.

An audit found 1,852 truncated verdicts, all at exactly 120 completion
tokens. Successful verdicts had p95 313 and maximum 1,050. The uncapped
user-filter path had 0 failures in 2,075 calls; the managed-board path had
1,725 in 11,629. A fail-closed board silently omits failed verdicts.
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
    from api import managed_boards as runs

    # 320 is the measured p95 of a successful verdict. A reservation smaller
    # than that under-holds budget for one run in twenty.
    assert runs.FILTER_OUTPUT_RESERVATION_TOKENS >= 320
