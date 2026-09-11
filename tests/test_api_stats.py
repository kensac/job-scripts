"""The board's numbers count the board, not the bookkeeping rows behind it."""

from __future__ import annotations

from api import db
from api.board import visibility
from tests.test_api_jobs import _insert_job, _pass_closed, _subscribe, _uid


def _counts(client, headers) -> dict[str, int]:
    # The membership the recompute task writes, which is what the board and
    # these counts read.
    visibility.recompute(_uid(headers))
    body = client.get("/v1/user/stats", headers=headers).json()
    return {r["status"]: r["count"] for r in body["by_status"]}


def test_the_to_apply_count_is_the_board_not_every_untouched_row(client, user_headers):
    """ "To apply" read 2,331 while the board held 578: the counter summed
    user_jobs rows with no status, and an untouched row outlives the posting's
    membership. A row the person acted on counts whatever the posting's
    verdicts say; an untouched one counts only while the posting is visible."""
    uid = _uid(user_headers)
    on_board = _insert_job("src-st", "https://x.test/st1")
    fell_off = _insert_job("src-st", "https://x.test/st2")
    applied_but_closed = _insert_job("src-st", "https://x.test/st3")
    _subscribe(uid, "src-st")
    for i in (1, 2, 3):
        _pass_closed(f"https://x.test/st{i}")
    for jid, status in ((on_board, None), (fell_off, None), (applied_but_closed, "Applied")):
        db.execute(
            "INSERT INTO user_jobs (user_id, job_id, status) VALUES (%s, %s, %s)",
            (uid, jid, status),
        )
    assert _counts(client, user_headers) == {"": 2, "Applied": 1}

    # The closed check turns on two of them: the untouched one leaves the
    # count, the applied one stays.
    from core.store import add_ai_result

    add_ai_result("https://x.test/st2", "rejected", check_type="closed")
    add_ai_result("https://x.test/st3", "rejected", check_type="closed")
    assert _counts(client, user_headers) == {"": 1, "Applied": 1}
    by_source = {
        r["source"]: r
        for r in client.get("/v1/user/stats", headers=user_headers).json()["by_source"]
    }
    assert by_source["src-st"]["total"] == 2 and by_source["src-st"]["with_status"] == 1
