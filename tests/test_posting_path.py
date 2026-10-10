"""A posting's path to each board and filter, and where it stopped."""

from __future__ import annotations

from api import db, posting_path
from core import catalog
from tests.test_verify_board_questions import _board


def _uid(email: str = "user@example.com") -> int:
    return db.query_one("SELECT id FROM users WHERE email = %s", (email,))["id"]


def _consumer(path, kind, name=None):
    return next(c for c in path.consumers if c.kind == kind and (name is None or c.name == name))


def _stages(consumer):
    return {s.stage: s for s in consumer.steps}


def _gate(scopes, **overrides):
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('verification_volume_gate', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb({"scopes": scopes, **overrides}),),
    )


def test_a_posting_on_a_board_shows_each_stage_it_passed(f):
    source = f.make_source("path-src")
    board = _board(f, source, slug="path-board")
    job_id, url = f.make_ready_job(source=source, title="Software Engineer")
    f.make_verdict(url, "custom", "passed", prompt_hash=board["prompt_hash"])
    db.execute(
        "UPDATE ai_queries SET model = 'gpt-6-luna', config_name = 'verify-batch' "
        "WHERE url = %s AND check_type = 'custom'",
        (url,),
    )
    db.execute(
        "INSERT INTO managed_board_jobs (managed_board_id, job_id, sort_at) VALUES (%s, %s, now())",
        (board["id"], job_id),
    )

    path = posting_path.for_admin(url)

    assert path is not None
    consumer = _consumer(path, "managed_board", "path-board")
    stages = _stages(consumer)
    assert consumer.included and consumer.summary == "On it"
    assert stages["criteria"].outcome == "passed" and stages["criteria"].basis == "evaluated_now"
    assert stages["structural"].outcome == "passed"
    assert stages["verdict"].label == "Kept"
    assert "answered when the posting was verified" in stages["verdict"].detail
    assert stages["membership"].outcome == "passed" and stages["membership"].basis == "recorded"
    assert {s.check for s in path.steps if s.stage == "verification"} == {"closed", "clearance"}


def test_outside_the_criteria_names_the_criterion(f):
    source = f.make_source("path-src-age")
    board = _board(f, source, slug="path-board-age")
    db.execute(
        "UPDATE managed_boards SET criteria = '{\"max_age_days\": 7}'::jsonb WHERE id = %s",
        (board["id"],),
    )
    job_id, url = f.make_ready_job(source=source)
    db.execute("UPDATE jobs SET date_posted = now() - interval '20 days' WHERE id = %s", (job_id,))

    consumer = _consumer(posting_path.for_admin(url), "managed_board", "path-board-age")

    assert consumer.included is False
    assert consumer.summary == "Outside the criteria"
    assert "inside the age window" in _stages(consumer)["criteria"].detail


def test_a_volume_skip_says_why_verification_never_read_it(f):
    source = f.make_source("path-src-occ")
    board = _board(f, source, slug="path-board-occ")
    _gate([board["prompt_hash"]])
    _, url = f.make_ready_job(source=source, title="Registered Nurse", closed="", clearance="")

    path = posting_path.for_admin(url)
    consumer = _consumer(path, "managed_board", "path-board-occ")

    assert _stages(consumer)["volume"].outcome == "skipped"
    assert "unrelated occupation" in consumer.summary
    assert [s.label for s in path.steps if s.stage == "verification"] == ["Not verified yet"]


def test_an_enforced_title_gate_drop_is_a_skip(f):
    source = f.make_source("path-src-gate")
    _board(
        f,
        source,
        slug="path-board-gate",
        title_gate={"recipe": "internship_v1", "mode": "enforce"},
    )
    _, url = f.make_ready_job(source=source, title="Senior Staff Engineer")

    consumer = _consumer(posting_path.for_admin(url), "managed_board", "path-board-gate")

    assert _stages(consumer)["title_gate"].outcome == "skipped"
    assert consumer.summary.startswith("Title gate internship_v1: dropped")


def test_a_title_screen_is_evaluated_now_and_writes_nothing(f, set_config):
    source = f.make_source("path-src-screen")
    board = _board(f, source, slug="path-board-screen")
    _, nurse = f.make_ready_job(source=source, title="Registered Nurse")
    _, engineer = f.make_ready_job(source=source, title="Software Engineer")
    set_config("title_screens", {board["prompt_hash"]: "nontechnical_occupations_v1"})

    skipped = _stages(
        _consumer(posting_path.for_admin(nurse), "managed_board", "path-board-screen")
    )
    judged = _stages(
        _consumer(posting_path.for_admin(engineer), "managed_board", "path-board-screen")
    )

    assert skipped["review_gate"].outcome == "skipped"
    assert skipped["review_gate"].basis == "evaluated_now"
    assert skipped["review_gate"].detail == "clinical_care"
    assert judged["review_gate"].outcome == "passed"
    set_config("title_screens", {})
    after = _stages(_consumer(posting_path.for_admin(nurse), "managed_board", "path-board-screen"))
    assert "review_gate" not in after


def test_a_near_copy_verdict_names_its_twin(f):
    source = f.make_source("path-src-twin")
    twin_id, twin = f.make_ready_job(source=source, title="Store Associate")
    copy_id, copy = f.make_ready_job(source=source, title="Store Associate")
    db.execute("UPDATE jobs SET near_copy_key = 'k' WHERE id IN (%s, %s)", (twin_id, copy_id))
    db.execute("UPDATE ai_queries SET config_name = 'verify-near-copy' WHERE url = %s", (copy,))

    path = posting_path.for_admin(copy)

    closed = next(s for s in path.steps if s.check == "closed")
    assert "copied from a twin posting" in closed.detail and twin in closed.detail


def test_a_person_sees_their_filter_reject_it_and_why_it_is_off_their_board(
    client, user_headers, f
):
    uid = _uid()
    source = f.make_source("path-src-user")
    f.subscribe(uid, source)
    filt = f.make_filter(uid)
    filter_id, prompt_hash = filt["id"], filt["prompt_hash"]
    job_id, url = f.make_ready_job(source=source)
    f.make_verdict(url, "custom", "rejected", prompt_hash=prompt_hash, reason="needs 5 years")
    f.make_board_row(uid, job_id)

    resp = client.get(f"/v1/user/jobs/{job_id}/path", headers=user_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    mine = next(c for c in body["consumers"] if c["kind"] == "filter")
    assert mine["included"] is False and mine["summary"] == "Rejected"
    verdict = next(s for s in mine["steps"] if s["stage"] == "verdict")
    assert verdict["filter_id"] == filter_id and "needs 5 years" in verdict["detail"]
    assert any(c["kind"] == "board" for c in body["consumers"])
    assert all(c["kind"] != "managed_board" for c in body["consumers"]), "boards are admin-only"


def test_a_person_cannot_read_the_path_of_a_posting_they_cannot_see(
    client, user_headers, other_user_headers, f
):
    other = _uid("user2@example.com")
    job_id = f.make_job(source="upload", uploaded_by=other)

    assert client.get(f"/v1/user/jobs/{job_id}/path", headers=user_headers).status_code == 404


def test_the_admin_route_requires_an_admin(client, user_headers, admin_headers, f):
    _, url = f.make_ready_job(source=f.make_source("path-src-admin"))

    assert (
        client.get("/v1/admin/jobs/path", params={"url": url}, headers=user_headers).status_code
        == 403
    )
    resp = client.get("/v1/admin/jobs/path", params={"url": url}, headers=admin_headers)
    assert resp.status_code == 200 and resp.json()["url"] == url


def test_the_catalog_step_reads_availability_not_the_feed_flag(f):
    """A posting another source still lists is available whatever the feed
    flag says; a sheet_import posting is not, whatever its flag says."""
    source = f.make_source("path-listing-src")
    listed_id, listed_url = f.make_ready_job(source=source, active=False)
    db.execute(
        "INSERT INTO source_observations (job_id, source, kind) VALUES (%s, %s, 'appeared')",
        (listed_id, source),
    )
    _, imported_url = f.make_ready_job(source="sheet_import")
    # What the hourly reconcile stores (catalog.reconcile_available).
    catalog.reconcile_available()

    def catalog_failures(url):
        path = posting_path.for_admin(url)
        assert path is not None
        return [s for s in path.steps if s.stage == "catalog" and s.outcome == "failed"]

    assert catalog_failures(listed_url) == []
    [step] = catalog_failures(imported_url)
    assert step.label == "No switched-on source lists it"
