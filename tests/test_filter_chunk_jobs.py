"""Where a filter chunk keeps its job list, and that every reader of it sees
the same jobs and URLs whichever shape the payload holds."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import ai, db, task_jobs
from core.payload_objects import PayloadStore, PayloadUnavailable
from core.store import add_ai_result
from tasks import filters
from tasks.board import in_flight_urls, submission_exclusions
from tasks.runtime import payload_recovery
from tests.factories import ObjectClient, make_task

CHUNKS = task_jobs.FILTER_CHUNKS


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
    return store


def _payload(task_id: int) -> dict:
    return db.query_one("SELECT payload FROM tasks WHERE id = %s", (task_id,))["payload"]


def _texts() -> dict[int, str]:
    return {row["id"]: row["t"] for row in db.query("SELECT id, payload::text AS t FROM tasks")}


def _jobs(*names: str) -> list[dict]:
    return [{"url": f"https://{n}", "company": f"Co {n}", "title": f"Role {n}"} for n in names]


def _chunk(jobs, *, uid=7, prompt_hash="criteria", status="running", kind=None, **extra):
    return make_task(
        kind or "run_filter_batch_chunk",
        {"user_id": uid, "filter": {"prompt_hash": prompt_hash}, "jobs": jobs, **extra},
        status=status,
    )


def _all(mode: str, objects, *, limit: int = 100, workers: int = 1) -> list[dict]:
    through = db.query_one("SELECT max(id) AS n FROM tasks")["n"]
    results, cursor = [], 0
    while not results or not results[-1]["exhausted"]:
        results.append(
            task_jobs.migrate(
                CHUNKS,
                mode,
                after=cursor,
                through=through,
                limit=limit,
                store=objects,
                workers=workers,
            )
        )
        cursor = results[-1]["after"]
    return results


def _count(results: list[dict], outcome: str) -> int:
    return sum(result["counts"].get(outcome, 0) for result in results)


def test_sql_readers_see_the_urls_of_a_referenced_chunk():
    # The shape a reference-only writer produces. The object is irrelevant to
    # these readers: they run in SQL and read only the inline URLs.
    referenced = {"jobs_ref": {"key": "unread"}, "candidate_count": 2}
    held = make_task(
        "run_filter_batch_chunk",
        {
            "user_id": 7,
            "filter": {"prompt_hash": "criteria"},
            "urls": ["https://a", "https://b"],
            **referenced,
        },
        status="awaiting_batch",
    )
    make_task(
        "run_filter_chunk", {"user_id": 7, "urls": ["https://d"], **referenced}, status="done"
    )
    make_task(
        "run_filter_chunk", {"user_id": 8, "urls": ["https://e"], **referenced}, status="running"
    )
    newer = _chunk(_jobs("a"))

    assert in_flight_urls(7) == {"https://a", "https://b"}
    assert submission_exclusions(newer, 7, ["https://a", "https://z"], "criteria", "m") == {
        "https://a"
    }
    assert submission_exclusions(held, 7, ["https://a"], "criteria", "m") == set()


def test_sql_readers_return_identical_results_before_and_after_externalizing(objects):
    """Every case the two readers distinguish, read once over the legacy inline
    payloads and again after the same chunks hold only a reference."""
    a, b, c, d, e, g = (f"https://{n}" for n in "abcdeg")
    _chunk(_jobs("a", "b"), status="awaiting_batch")
    _chunk(_jobs("c"), status="pending", kind="run_filter_chunk")
    _chunk(_jobs("d"), status="done")
    _chunk(_jobs("e"), status="failed", batch_ids=["paid"])
    _chunk(_jobs("g"), status="running", uid=8)
    _chunk(_jobs("b"), status="waiting", prompt_hash="other")
    newest = _chunk(_jobs("a", "b", "c", "d", "e", "g"), status="cancelled")
    add_ai_result(g, "passed", None, "custom", prompt_hash="criteria", model="m")
    urls = [a, b, c, d, e, g]

    def read():
        return (
            in_flight_urls(7),
            in_flight_urls(8),
            submission_exclusions(newest, 7, urls, "criteria", "m"),
            submission_exclusions(newest, 7, urls, "other", "m"),
        )

    before = read()
    assert before == ({a, b, c}, {g}, {a, b, c, e, g}, {b})

    assert _count(_all("externalize", objects), "externalized") == 7
    assert all("jobs" not in _payload(task_id) for task_id in _texts())
    assert read() == before

    _all("restore", objects)
    assert read() == before


def test_externalize_keeps_the_urls_inline_and_restore_puts_back_the_exact_payload(objects):
    jobs = _jobs("a", "b")
    task_id = _chunk(jobs, parent_id=3, scheduled=True, batch_ids=["x"])
    other = make_task("run_managed_board_batch", {"managed_board_id": 1, "jobs": jobs})
    original = _texts()

    [result] = _all("externalize", objects)

    assert result["counts"] == {"externalized": 1} and not result["failed"]
    payload = _payload(task_id)
    assert payload["urls"] == ["https://a", "https://b"] and payload["candidate_count"] == 2
    assert task_jobs.run_jobs(payload, objects) == jobs
    rest = {k: v for k, v in payload.items() if k not in ("jobs_ref", "candidate_count", "urls")}
    assert rest == {k: v for k, v in json.loads(original[task_id]).items() if k != "jobs"}
    assert _texts()[other] == original[other]

    assert _count(_all("count", objects), "referenced") == 1
    assert _count(_all("verify", objects), "verified") == 1
    assert _count(_all("restore", objects), "restored") == 1
    assert _texts() == original


def test_verify_refuses_urls_that_disagree_with_the_object(objects):
    tampered = _chunk(_jobs("a", "b"))
    no_urls = _chunk(_jobs("c"))
    _all("externalize", objects)
    db.execute(
        "UPDATE tasks SET payload = jsonb_set(payload, '{urls}', '[\"https://b\", \"https://a\"]') "
        "WHERE id = %s",
        (tampered,),
    )
    db.execute("UPDATE tasks SET payload = payload - 'urls' WHERE id = %s", (no_urls,))

    [result] = _all("verify", objects)

    assert result["counts"] == {"unavailable": 1, "conflict": 1}
    assert result["failed"] == [tampered, no_urls]
    with pytest.raises(PayloadUnavailable):
        task_jobs.run_jobs(_payload(tampered), objects)


@pytest.mark.asyncio
async def test_batch_chunk_handler_reads_its_jobs_from_the_reference(objects, monkeypatch):
    jobs = _jobs("a")
    task = _chunk(jobs, parent_id=None, scheduled=True)
    _all("externalize", objects)
    payload = _payload(task)
    assert "jobs" not in payload
    payload["filter"].update(name="test", prompt="criteria", on_ambiguous="filter")
    cfg = ai.AIConfig("openai", "test", "owner", "m")
    monkeypatch.setattr(filters, "load_config", lambda *args: (None, cfg))
    monkeypatch.setattr(filters.batch_policy, "transport", lambda *args: "batch")
    prepared = []

    async def prepare(task_id, chunk_jobs, **kwargs):
        prepared.append(chunk_jobs)
        add_ai_result("https://a", "passed", None, "custom", prompt_hash="criteria", model="m")
        return {"https://a": "content"}, 0

    monkeypatch.setattr(filters, "prepare_content", prepare)
    monkeypatch.setattr(
        filters,
        "_personal_hooks",
        lambda *args, **kwargs: SimpleNamespace(progress=lambda *a: None, complete=lambda: None),
    )

    await filters.handle_run_filter_batch_chunk(task, payload)

    assert prepared == [jobs]


def test_an_unreadable_chunk_list_is_held_for_payload_recovery(objects):
    task = _chunk(_jobs("a"), status="failed")
    _all("externalize", objects)
    db.execute(
        "UPDATE tasks SET payload = payload || "
        '\'{"payload_recovery":{"reason":"payload_unavailable"}}\' WHERE id = %s',
        (task,),
    )
    objects.client.fail_get = True
    assert payload_recovery.retry(task, objects) == "unavailable"
    objects.client.fail_get = False
    assert payload_recovery.retry(task, objects) == "pending"
