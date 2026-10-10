"""posting_uploads follows jobs.uploaded_by and jobs.extraction_status while
both are written, and the backfill brings rows an older server wrote into
line."""

from __future__ import annotations

import asyncio

from api import db
from core import catalog
from core.fetching.posting import JobPosting
from tasks import posting_uploads


def _uploads() -> dict[int, tuple[int, str]]:
    return {
        r["job_id"]: (r["uploaded_by"], r["status"])
        for r in db.query("SELECT job_id, uploaded_by, status FROM posting_uploads")
    }


def test_every_upload_state_write_reaches_posting_uploads(f):
    uid = f.make_user()
    job_id = catalog.add_upload("https://x.test/up", "https://x.test/up", uid)["id"]
    assert _uploads() == {job_id: (uid, "pending")}
    catalog.set_extraction_status(job_id, "failed")
    assert _uploads() == {job_id: (uid, "failed")}
    catalog.add_upload("https://x.test/up", "https://x.test/up", uid)
    assert _uploads() == {job_id: (uid, "pending")}
    catalog.record_extraction(job_id, "Acme", "Engineer", [], [])
    assert _uploads() == {job_id: (uid, "done")}


def test_saving_a_catalog_posting_or_reparsing_one_writes_no_upload(f):
    uid = f.make_user()
    job_id = f.make_job(url="https://x.test/listed")
    assert catalog.add_upload("https://x.test/listed", "https://x.test/listed", uid)["id"] == job_id
    catalog.set_extraction_status(job_id, "pending")
    catalog.record_extraction(job_id, "Acme", "Engineer", [], [])
    assert _uploads() == {}


def test_a_pull_that_takes_an_upload_over_marks_it_done(f):
    uid = f.make_user()
    url = "https://job-boards.greenhouse.io/rocketlab/jobs/1"
    job_id = catalog.add_upload(url, url, uid)["id"]
    posting = JobPosting(
        company="Rocket Lab",
        locations=[],
        title="Engineer",
        url=url,
        terms=[],
        active=True,
        date_posted=0,
        raw_url="",
    )
    catalog.upsert_postings([posting], "rocketlab")
    assert _uploads() == {job_id: (uid, "done")}


def test_backfill_copies_what_an_older_server_wrote_and_then_finds_nothing(f):
    uid = f.make_user()
    # What a server on the release before posting_uploads writes: jobs only.
    missing = f.make_job()
    db.execute("UPDATE jobs SET uploaded_by = %s WHERE id = %s", (uid, missing))
    behind = catalog.add_upload("https://x.test/behind", "https://x.test/behind", uid)["id"]
    db.execute("UPDATE jobs SET extraction_status = 'done' WHERE id IN (%s, %s)", (missing, behind))
    # Not an upload: the sheet import stamped extraction done with no uploader.
    f.make_job(source="sheet_import")
    db.execute("UPDATE jobs SET extraction_status = 'done' WHERE source = 'sheet_import'")

    first = f.make_task("backfill_posting_uploads", status="running")
    asyncio.run(posting_uploads.handle_backfill_posting_uploads(first, {}))
    assert _uploads() == {missing: (uid, "done"), behind: (uid, "done")}
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (first,))
    assert progress is not None and progress["progress"]["written"] == 2

    second = f.make_task("backfill_posting_uploads", status="running")
    asyncio.run(posting_uploads.handle_backfill_posting_uploads(second, {}))
    progress = db.query_one("SELECT progress FROM tasks WHERE id = %s", (second,))
    assert progress is not None and progress["progress"]["written"] == 0


def test_readers_take_ownership_from_posting_uploads_alone(f):
    """jobs.uploaded_by is left NULL, so a reader still on it would treat the
    upload as public catalog: reachable by a stranger, not its owner's."""
    from api import posting_path
    from api.board import person_state, visibility

    owner, stranger = f.make_user(), f.make_user()
    job_id = f.make_job(source="upload")
    db.execute(
        "INSERT INTO posting_uploads (job_id, uploaded_by, status) VALUES (%s, %s, 'failed')",
        (job_id, owner),
    )

    assert job_id in visibility.member_ids(owner)
    assert job_id not in visibility.member_ids(stranger)
    assert person_state.touchable_job_ids(owner, [job_id]) == {job_id}
    assert person_state.touchable_job_ids(stranger, [job_id]) == set()
    path = posting_path.for_user(job_id, owner)
    assert path is not None and path.consumers[-1].included
    assert job_id in {r["id"] for r in db.query(visibility.across_users("j.id"))}
