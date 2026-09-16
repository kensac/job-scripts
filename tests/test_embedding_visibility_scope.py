import pytest

from api import db
from api.board import visibility
from core import batch
from tasks import embeddings, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_only", [True, False])
async def test_new_embedding_submissions_follow_visibility_with_reversible_scope(
    f, monkeypatch, visible_only
):
    uid = f.make_user()
    source = f.make_source()
    f.subscribe(uid, source)
    urls = [f"https://embedding-scope.test/{i}" for i in range(6)]
    jobs = [f.make_job(url=url, source=source) for url in urls[:5]]
    for url in urls:
        f.make_verdict(url, "content", content=f"Detailed posting at {url} " * 30)
    db.execute("INSERT INTO board_visible(user_id,job_id) VALUES (%s,%s)", (uid, jobs[0]))
    db.execute("UPDATE jobs SET uploaded_by=%s WHERE id=%s", (uid, jobs[1]))
    db.execute(
        "INSERT INTO user_jobs(user_id,job_id,notes) VALUES (%s,%s,'follow up')",
        (uid, jobs[2]),
    )
    db.execute("INSERT INTO user_jobs(user_id,job_id) VALUES (%s,%s)", (uid, jobs[3]))
    db.execute(
        "INSERT INTO app_config(key,value) VALUES ('embedding_visible_only',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (db.jsonb(visible_only),),
    )
    task = f.make_task("embed_postings_batch", {}, status="running")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    submitted = []

    async def submit(specs, model, effort, max_output, on_event=None):
        submitted.extend(specs)
        return ["scope-batch"]

    monkeypatch.setattr(batch, "submit_responses_batches", submit)
    with pytest.raises(runtime.AwaitingBatch):
        await embeddings.handle_embed_postings_batch(task, {})
    actual = {row["url"] for spec in submitted for row in spec.context["rows"]}
    assert actual == set(urls[:3] if visible_only else urls)


def test_scope_tracks_visibility_changes_and_matches_personal_reads(f):
    owners = [f.make_user(), f.make_user()]
    jobs = [f.make_job() for _ in range(4)]
    db.execute("UPDATE jobs SET uploaded_by=%s WHERE id=%s", (owners[0], jobs[0]))
    db.execute(
        "INSERT INTO user_jobs(user_id,job_id,status) VALUES (%s,%s,'Applied')",
        (owners[1], jobs[1]),
    )
    db.execute("INSERT INTO user_jobs(user_id,job_id) VALUES (%s,%s)", (owners[0], jobs[2]))
    db.execute("INSERT INTO board_visible(user_id,job_id) VALUES (%s,%s)", (owners[1], jobs[3]))

    def check(expected):
        actual = {r["id"] for r in db.query(visibility.across_users("j.id"))}
        individual = {
            r["id"]
            for uid in owners
            for r in db.query(visibility.FAST.format(columns="j.id", extra=""), {"uid": uid})
        }
        assert actual == individual == set(expected)

    check([jobs[0], jobs[1], jobs[3]])
    db.execute("INSERT INTO board_visible(user_id,job_id) VALUES (%s,%s)", (owners[0], jobs[2]))
    check(jobs)
    db.execute("DELETE FROM board_visible WHERE job_id=%s", (jobs[3],))
    check(jobs[:3])
