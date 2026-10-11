"""The title-screen funnel is derived from postings and verdicts, never stored."""

from __future__ import annotations

from decimal import Decimal

from api import db
from tests.test_verify_board_questions import _board


def test_the_funnel_counts_screened_postings_and_paid_reviews(client, admin_headers, f, set_config):
    source = f.make_source("funnel-src")
    board = _board(f, source, slug="funnel-board")
    other = f.make_source("funnel-elsewhere")
    f.make_job(source=source, title="Registered Nurse")
    f.make_job(source=source, title="Barista")  # not on this recipe's list
    f.make_job(source=other, title="Registered Nurse")  # not the board's source
    _, url = f.make_ready_job(source=source, title="Software Engineer")
    for cost in ("0.002", "0.004"):
        # A review and the call that paid for it, at the cost it recorded.
        f.paid_answer(
            url,
            check_type="custom",
            model="gpt-5-nano",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            prompt_hash=board["prompt_hash"],
        )
        db.execute(
            "UPDATE model_calls SET cost_usd = %s WHERE id = (SELECT max(id) FROM model_calls)",
            (Decimal(cost),),
        )
    set_config("title_screens", {board["prompt_hash"]: "nontechnical_occupations_v1"})

    body = client.get("/v1/admin/review-gates/report?days=7", headers=admin_headers).json()

    rows = {row["action"]: row for row in body["rows"]}
    assert rows["skip"]["decisions"] == 1 and rows["skip"]["distinct_jobs"] == 1
    assert rows["review"]["decisions"] == 2 and rows["review"]["distinct_jobs"] == 1
    assert rows["review"]["actual_cost_usd"] == 0.006
    assert body["avoided_cost"]["estimated_avoided_cost_usd"] == 0.003
    assert body["avoided_cost"]["estimated_decisions"] == 1

    scoped = client.get(
        f"/v1/admin/review-gates/report?days=7&managed_board_id={board['id'] + 1}",
        headers=admin_headers,
    ).json()
    assert scoped["rows"] == []

    set_config("title_screens", {})
    off = client.get("/v1/admin/review-gates/report?days=7", headers=admin_headers).json()
    assert off["rows"] == []
