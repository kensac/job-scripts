"""posting_uploads is the only record of an upload, and the clearing task
empties the jobs columns it replaced without losing an owner."""

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


def _jobs_columns() -> dict[int, tuple[int | None, str | None]]:
    return {
        r["id"]: (r["uploaded_by"], r["extraction_status"])
        for r in db.query(
            "SELECT id, uploaded_by, extraction_status FROM jobs "
            "WHERE uploaded_by IS NOT NULL OR extraction_status IS NOT NULL"
        )
    }


def _run_clear(f) -> dict:
    task_id = f.make_task("clear_upload_columns", status="running")
    asyncio.run(posting_uploads.handle_clear_upload_columns(task_id, {}))
    row = db.query_one("SELECT progress FROM tasks WHERE id = %s", (task_id,))
    assert row is not None
    return row["progress"]


def test_the_upload_writers_leave_the_jobs_columns_alone(f):
    uid = f.make_user()
    job_id = catalog.add_upload("https://x.test/cols", "https://x.test/cols", uid)["id"]
    catalog.set_extraction_status(job_id, "failed")
    catalog.record_extraction(job_id, "Acme", "Engineer", [], [])
    assert _jobs_columns() == {}


def test_clearing_keeps_every_owner_and_every_stamp_then_finds_nothing(f, monkeypatch):
    uid, other = f.make_user(), f.make_user()
    # What a server on the release before this one wrote: both copies.
    upload = f.make_job(source="upload")
    f.upload(upload, uid)
    db.execute(
        "UPDATE jobs SET uploaded_by = %s, extraction_status = 'done' WHERE id = %s", (uid, upload)
    )
    # Its status written to jobs on an upload whose jobs row names nobody.
    status_only = f.make_job(source="upload")
    f.upload(status_only, uid, status="failed")
    db.execute("UPDATE jobs SET extraction_status = 'failed' WHERE id = %s", (status_only,))
    # Not an upload: the sheet import's stamp is the only record of it.
    stamped = f.make_job(source="sheet_import")
    db.execute("UPDATE jobs SET extraction_status = 'done' WHERE id = %s", (stamped,))
    # An owner posting_uploads does not name is not cleared.
    orphan = f.make_job(source="upload")
    f.upload(orphan, uid)
    db.execute("UPDATE jobs SET uploaded_by = %s WHERE id = %s", (other, orphan))
    # One upload per statement, so the walk crosses chunk boundaries.
    monkeypatch.setattr(posting_uploads, "BATCH", 1)

    first = _run_clear(f)
    assert (first["cleared"], first["unmatched"]) == (2, 1)
    assert _jobs_columns() == {orphan: (other, None), stamped: (None, "done")}
    assert _uploads() == {
        upload: (uid, "done"),
        status_only: (uid, "failed"),
        orphan: (uid, "done"),
    }

    second = _run_clear(f)
    assert (second["cleared"], second["unmatched"]) == (0, 1)


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
