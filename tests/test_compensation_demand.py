from types import SimpleNamespace

import pytest

from api import db
from tasks import comp


@pytest.mark.asyncio
async def test_comp_waits_for_current_filter_pass_or_personal_tracking(f, monkeypatch):
    db.execute("UPDATE app_config SET value='true' WHERE key='compensation_demand_gate_enabled'")
    user = f.make_user()
    source = f.make_source()
    f.subscribe(user, source)
    enabled = f.make_filter(user)
    _, passed = f.make_ready_job(source=source)
    _, rejected = f.make_ready_job(source=source)
    tracked_id, tracked = f.make_ready_job(source=source)
    untouched_id, untouched = f.make_ready_job(source=source)
    _, unknown = f.make_ready_job(source=source)
    f.make_verdict(passed, "custom", prompt_hash=enabled["prompt_hash"])
    f.make_verdict(rejected, "custom", prompt_hash=enabled["prompt_hash"])
    f.make_verdict(rejected, "custom", "rejected", prompt_hash=enabled["prompt_hash"])
    f.make_board_row(user, tracked_id)
    f.make_board_row(user, untouched_id, status=None)
    asked = []

    async def collect(task_id, shape, specs):
        asked.extend(s.custom_id for s in specs)
        return [], SimpleNamespace()

    monkeypatch.setattr(comp, "run_batched", collect)
    await comp.handle_extract_comp(f.make_task("extract_comp"), {})
    assert set(asked) == {passed, tracked}
    assert not set(asked) & {rejected, untouched, unknown}
    db.execute("UPDATE user_filters SET enabled=false WHERE id=%s", (enabled["id"],))
    asked.clear()
    await comp.handle_extract_comp(f.make_task("extract_comp"), {})
    assert asked == [tracked]


@pytest.mark.asyncio
async def test_comp_admits_a_published_managed_board_without_personal_subscription(f, monkeypatch):
    db.execute("UPDATE app_config SET value='true' WHERE key='compensation_demand_gate_enabled'")
    source = f.make_source()
    job, url = f.make_ready_job(source=source)
    sponsor = f.make_user()
    board = db.query_one(
        "INSERT INTO managed_boards (slug,name,sponsor_user_id,prompt,prompt_hash,"
        "requested_model,published,public_revision,published_at) "
        "VALUES ('comp-test','Comp',%s,'test','hash','test',true,1,now()) RETURNING id",
        (sponsor,),
    )
    db.execute(
        "INSERT INTO managed_board_jobs (managed_board_id,job_id,sort_at,projection_revision) "
        "VALUES (%s,%s,now(),1)",
        (board["id"], job),
    )
    asked = []

    async def collect(task_id, shape, specs):
        asked.extend(s.custom_id for s in specs)
        return [], SimpleNamespace()

    monkeypatch.setattr(comp, "run_batched", collect)
    await comp.handle_extract_comp(f.make_task("extract_comp"), {})
    assert asked == [url]
    db.execute("UPDATE managed_boards SET published=false WHERE id=%s", (board["id"],))
    asked.clear()
    await comp.handle_extract_comp(f.make_task("extract_comp"), {})
    assert asked == []
