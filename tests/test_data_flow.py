"""GET /admin/data-flow: each posting counted at the first gate it stops at."""

from __future__ import annotations

from api import db
from api.routers.admin import data_flow


def test_a_board_counts_each_posting_at_the_first_gate_it_stops_at(f):
    uid = f.make_user()
    source = f.make_source()
    board = db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model, title_gate) VALUES ('flow', 'Flow', %s, 'interns', 'flow-hash', "
        "'claude-haiku-5-5', %s) RETURNING id",
        (uid, db.jsonb({"mode": "enforce", "recipe": "internship_v1"})),
    )["id"]
    db.execute(
        "INSERT INTO managed_board_sources (managed_board_id, source) VALUES (%s, %s)",
        (board, source),
    )

    def job(title: str, *, active: bool = True, **verdicts: str) -> int:
        job_id = f.make_job(source=source, title=title, active=active)
        url = db.query_one("SELECT url FROM jobs WHERE id = %s", (job_id,))["url"]
        for check, status in verdicts.items():
            f.make_verdict(
                url,
                check,
                status,
                prompt_hash="flow-hash" if check == "custom" else None,
                reason=f"{check} said {status}",
            )
        return job_id

    job("Software Engineering Intern", active=False)
    job("Senior Staff Engineer", closed="passed", clearance="passed", custom="passed")
    job("Data Intern")
    job("Hardware Intern", closed="passed", clearance="rejected")
    job("Quant Intern", closed="passed", clearance="passed", custom="rejected")
    listed = job("Design Intern", closed="passed", clearance="passed", custom="passed")
    job("Ops Intern", closed="passed", clearance="passed", custom="passed")
    db.execute(
        "INSERT INTO managed_board_jobs (managed_board_id, job_id, sort_at) VALUES (%s, %s, now())",
        (board, listed),
    )

    flow = data_flow._flow(24, board).board
    assert flow is not None
    counts = {s.stage: s.count for s in flow.stages}
    assert counts == {
        "retired": 1,
        # Kept by the model, but the title gate comes first: a gated posting
        # is never verified or judged for the board.
        "title_gate": 1,
        "awaiting_open_check": 1,
        "restricted": 1,
        "rejected": 1,
        "kept_not_listed": 1,
        "listed": 1,
    }
    assert [s.stage for s in flow.stages] == [
        stage for stage in data_flow.STAGES if stage in counts
    ]
    why = {s.stage: s.samples[0].why for s in flow.stages}
    assert why["title_gate"] == "no_internship_title_signal"
    assert why["restricted"] == "clearance said rejected"
    assert why["rejected"] == "custom said rejected"
    assert flow.entered == 7


def test_the_window_arrives_as_a_query_string(client, admin_headers):
    # A Literal[24, 168, 720] parameter refused "168" from the query string,
    # so the page could never load.
    ok = client.get("/v1/admin/data-flow?hours=168", headers=admin_headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["hours"] == 168
    assert client.get("/v1/admin/data-flow?hours=5", headers=admin_headers).status_code == 422
