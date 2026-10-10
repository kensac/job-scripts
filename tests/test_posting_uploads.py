"""posting_uploads is the only record of an upload; jobs.extraction_status
is frozen."""

from __future__ import annotations

from api import db
from core import catalog
from core.fetching.posting import JobPosting


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


def test_the_upload_writers_leave_extraction_status_alone(f):
    uid = f.make_user()
    job_id = catalog.add_upload("https://x.test/cols", "https://x.test/cols", uid)["id"]
    catalog.set_extraction_status(job_id, "failed")
    catalog.record_extraction(job_id, "Acme", "Engineer", [], [])
    assert db.query("SELECT id FROM jobs WHERE extraction_status IS NOT NULL") == []


def test_readers_take_ownership_from_posting_uploads_alone(f):
    """The jobs row names no owner, so a reader that looked anywhere but
    posting_uploads would treat the upload as public catalog."""
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
