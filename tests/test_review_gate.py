import pytest

from api import ai, db, review_gate
from core.batch import structured_response_spec
from core.job_profile import (
    CLASSIFIER_VERSION,
    JOB_PROFILE_INSTRUCTIONS,
    JOB_PROFILE_MODEL,
    build_job_profile_input,
)
from core.review_gate import ReviewGatePolicy, profile_rejection, title_rejection
from tasks import filter_execution, job_profiles
from tasks.runtime import consume_result
from tests.factories import make_batch_result
from tests.test_filter_routing import hooks, profile


def configure(title="enforce", shared="off"):
    policy = ReviewGatePolicy.model_validate(
        {
            "title_mode": title,
            "profile_mode": shared,
            "scopes": {
                "test-hash": {
                    "title_recipe": "nontechnical_occupations_v1",
                    "profile_recipe": "nontechnical_families_v1",
                }
            },
        }
    )
    db.execute(
        "INSERT INTO app_config(key,value) VALUES ('filter_review_gate',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (db.jsonb(policy.model_dump(mode="json")),),
    )


@pytest.mark.parametrize(
    "title",
    [
        "Registered Nurse (RN) - ICU",
        "Retail Sales Associate - Part Time",
        "Delivery Driver (123) - Main Street",
        "JANITORIAL CLEANER",
        "Phlebotomist II",
    ],
)
def test_explicit_unrelated_occupations_reject(title):
    assert title_rejection(title) is not None


@pytest.mark.parametrize(
    "title",
    [
        "",
        "Analyst",
        "Operations Associate",
        "Program Manager",
        "Technician",
        "Device Driver Software Engineer",
        "Data Engineer - Retail Sales Associate Tools",
        "Product Manager - Nurse Platform",
        "Research Scientist",
        "ML Infrastructure Intern",
        "Finance Data Analyst",
        "Registered Nurse - Clinical Systems Analyst",
    ],
)
def test_ambiguous_or_technical_titles_abstain(title):
    assert title_rejection(title) is None


def test_profiles_reject_only_explicit_nontechnical_families():
    assert profile_rejection(
        profile(primary_role_family="legal", role_tracks=["other"]), "Legal Counsel"
    )
    for fields in (
        {"career_stage": "mid"},
        {"primary_role_family": "unknown", "role_tracks": ["other"]},
        {"primary_role_family": "sales", "role_tracks": ["unknown"]},
        {"primary_role_family": "sales", "role_tracks": []},
        {"primary_role_family": "finance", "role_tracks": ["analytics"]},
    ):
        assert profile_rejection(profile(**fields), "Analyst") is None
    assert (
        profile_rejection(
            profile(primary_role_family="sales", role_tracks=["other"]), "Solutions Engineer"
        )
        is None
    )


def proven_job(f):
    _, url = f.make_ready_job(content="exact posting content", title="Legal Counsel")
    job = db.query_one("SELECT url,title,company FROM jobs WHERE url=%s", (url,))
    content = db.query_one(
        "SELECT id FROM ai_queries WHERE url=%s AND check_type='content'", (url,)
    )
    context = {
        "url": url,
        "content_row_id": content["id"],
        "content_hash": job_profiles._content_hash("exact posting content"),
        "classifier_version": CLASSIFIER_VERSION,
    }
    answer = profile(primary_role_family="legal", role_tracks=["other"])
    task = f.make_task("classify_job_profiles")
    spec = structured_response_spec(
        str(content["id"]),
        JOB_PROFILE_INSTRUCTIONS,
        build_job_profile_input(job["title"], "exact posting content"),
        type(answer),
        context=context,
    )
    result = make_batch_result(task, spec, text=answer.model_dump_json(), model=JOB_PROFILE_MODEL)
    with consume_result(task, result) as receipt:
        job_profiles._store(url, context, answer, JOB_PROFILE_MODEL)
        receipt.outcome = "written"
    return job, task


def test_profile_reuse_requires_proven_title_content_response_and_model(f):
    job, task = proven_job(f)
    content = {job["url"]: "exact posting content"}
    assert job["url"] in review_gate.proven_profiles([job], content, 1000)
    assert not review_gate.proven_profiles([{**job, "title": "Changed title"}], content, 1000)
    assert not review_gate.proven_profiles([job], {job["url"]: "changed content"}, 1000)
    db.execute("UPDATE batch_result_receipts SET model='unknown' WHERE task_id=%s", (task,))
    assert not review_gate.proven_profiles([job], content, 1000)
    db.execute(
        "UPDATE batch_result_receipts SET model=%s WHERE task_id=%s", (JOB_PROFILE_MODEL, task)
    )
    db.execute(
        "UPDATE batch_requests SET snapshot=snapshot || %s WHERE task_id=%s",
        (
            db.jsonb({"input": "different original input"}),
            task,
        ),
    )
    assert not review_gate.proven_profiles([job], content, 1000)


def test_independent_controls_revision_scope_rollback_and_no_fake_verdict(f):
    job, _ = proven_job(f)
    task = f.make_task("run_filter_batch_chunk")
    content = {job["url"]: "exact posting content"}
    configure(title="off", shared="enforce")
    kept, decisions = review_gate.partition(task, "test-hash", [job], content)
    assert kept == []
    assert decisions[job["url"]]["stage"] == "profile"
    assert review_gate.partition(task, "edited-hash", [job], content)[0] == [job]
    configure(title="off", shared="off")
    assert review_gate.partition(task, "test-hash", [job], content)[0] == [job]
    assert db.query_one("SELECT count(*) n FROM ai_queries WHERE check_type='custom'")["n"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("off", 2), ("shadow", 2), ("enforce", 1)])
async def test_batch_gate_changes_requests_not_verdict_cache(f, mode, expected):
    configure(title=mode)
    jobs = [
        {"url": "https://example.test/nurse", "title": "Registered Nurse", "company": "Example"},
        {
            "url": "https://example.test/engineer",
            "title": "Software Engineer",
            "company": "Example",
        },
    ]
    task = f.make_task("run_filter_batch_chunk", status="running")
    submitted = []

    async def submit(task_id, specs, *_args):
        submitted.extend(specs)
        return [
            make_batch_result(task_id, s, text='{"should_filter":false}', model=JOB_PROFILE_MODEL)
            for s in specs
        ]

    await filter_execution.execute_batch(
        task,
        ai.AIConfig("openai", "key", "owner", JOB_PROFILE_MODEL),
        filter_execution.FilterSnapshot("test", "prompt", "filter", "test-hash"),
        jobs,
        hooks(),
        contents={j["url"]: "content" for j in jobs},
        unavailable=0,
        submit=submit,
    )
    assert len(submitted) == expected
    assert (
        db.query_one("SELECT count(*) n FROM ai_queries WHERE check_type='custom'")["n"] == expected
    )
    payload = db.query_one("SELECT payload FROM tasks WHERE id=%s", (task,))["payload"]
    assert payload["review_gate"]["detailed"] == expected
    if mode == "shadow":
        assert payload["review_gate_comparison"] == {"false_reject": 1}


@pytest.mark.asyncio
async def test_paid_batches_never_consult_gate(f, monkeypatch):
    task = f.make_task("run_filter_batch_chunk", status="running")

    def forbidden(*_args):
        raise AssertionError("paid batches cannot be replanned")

    monkeypatch.setattr(filter_execution, "has_batch_work", lambda _: True)
    monkeypatch.setattr(review_gate, "partition", forbidden)

    async def collect(*_args):
        return []

    await filter_execution.execute_batch(
        task,
        None,
        filter_execution.FilterSnapshot("test", "prompt", "filter", "test-hash"),
        [],
        hooks(),
        contents={},
        unavailable=0,
        collect=collect,
    )


def test_profile_lookup_failure_retains_detailed_review(f, monkeypatch):
    configure(title="off", shared="enforce")

    def fail(*_args):
        raise RuntimeError("lookup unavailable")

    monkeypatch.setattr(review_gate, "proven_profiles", fail)
    job = {"url": "https://example.test", "title": "Analyst"}
    task = f.make_task("run_filter_batch_chunk")
    assert review_gate.partition(task, "test-hash", [job], {job["url"]: "content"})[0] == [job]


@pytest.mark.asyncio
async def test_live_gate_skips_before_content_fetch(f, monkeypatch):
    configure()
    task = f.make_task("run_filter_chunk", status="running")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("excluded titles cannot fetch or call a provider")

    monkeypatch.setattr(filter_execution, "get_content", forbidden)
    monkeypatch.setattr(filter_execution, "check_filter", forbidden)
    await filter_execution.execute_live(
        task,
        ai.AIConfig("openai", "key", "owner", JOB_PROFILE_MODEL),
        filter_execution.FilterSnapshot("test", "prompt", "filter", "test-hash"),
        [{"url": "https://example.test/nurse", "title": "Registered Nurse"}],
        hooks(),
    )
    plan = db.query_one("SELECT payload->'review_gate' plan FROM tasks WHERE id=%s", (task,))[
        "plan"
    ]
    assert plan["detailed"] == 0


@pytest.mark.asyncio
async def test_all_excluded_batch_completes_without_submission(f):
    from dataclasses import replace

    configure()
    task = f.make_task("run_filter_batch_chunk", status="running")
    completed = []

    async def forbidden(*_args):
        raise AssertionError("empty review selection must not submit")

    await filter_execution.execute_batch(
        task,
        ai.AIConfig("openai", "key", "owner", JOB_PROFILE_MODEL),
        filter_execution.FilterSnapshot("test", "prompt", "filter", "test-hash"),
        [{"url": "https://example.test/nurse", "title": "Registered Nurse"}],
        replace(hooks(), complete=lambda: completed.append(True)),
        contents={},
        unavailable=1,
        submit=forbidden,
    )
    assert completed == [True]


def test_managed_fail_open_projection_excludes_gate_rejects_and_rollback_restores(f):
    from api import managed_board_runs

    sponsor = f.make_user()
    job_id = f.make_job(title="Registered Nurse")
    job = db.query_one("SELECT id,url,title,company FROM jobs WHERE id=%s", (job_id,))
    board = db.query_one(
        "INSERT INTO managed_boards(slug,name,sponsor_user_id,prompt,prompt_hash,requested_model) "
        "VALUES ('gate','Gate',%s,'prompt','test-hash',%s) RETURNING id",
        (sponsor, JOB_PROFILE_MODEL),
    )
    payload = {
        "managed_board_id": board["id"],
        "revision": 1,
        "prompt_hash": "test-hash",
        "requested_model": JOB_PROFILE_MODEL,
        "fail_closed": False,
        "jobs": [{**job, "sort_at": "2026-09-01T00:00:00+00:00"}],
    }
    task = f.make_task("run_managed_board_batch", payload)
    configure()
    assert review_gate.partition(task, "test-hash", [job], {})[0] == []
    assert managed_board_runs.replace_projection(task, payload) == 0
    configure(title="off")
    assert review_gate.partition(task, "test-hash", [job], {})[0] == [job]
    assert managed_board_runs.replace_projection(task, payload) == 1


def test_optional_comparison_failure_does_not_abort_receipt_transaction(f):
    task = f.make_task(
        "run_filter_batch_chunk", {"review_gate_comparison": {"false_reject": "bad"}}
    )
    with db.transaction():
        review_gate.record_comparison(task, {"stage": "title"}, False)
        db.execute("UPDATE tasks SET error='still writable' WHERE id=%s", (task,))
    assert db.query_one("SELECT error FROM tasks WHERE id=%s", (task,))["error"] == "still writable"
