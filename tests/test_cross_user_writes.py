"""A person's request can change only that person's state.

Each test acts as a user who is not the owner of what it touches: either a
shared verdict every board reads, or another person's private upload.
"""

from __future__ import annotations

import pytest

from api import ai, db
from api.ai import access as ai_access
from api.ai import verdicts
from api.board.eligibility import LATEST_CHECK
from core.store import Page

SECRET_URL = "https://private.test/secret-role"
SECRET_TEXT = "the secret posting text"


def _uid(headers: dict) -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (headers["X-User-Sub"],))
    assert row is not None
    return row["id"]


def _shared_closed(url: str) -> str | None:
    """The closed verdict as every board reads it."""
    row = db.query_one(
        f"WITH {LATEST_CHECK} SELECT status FROM latest_check "
        "WHERE url = %s AND check_type = 'closed'",
        (url,),
    )
    return row["status"] if row else None


def _rechecks() -> list[dict]:
    return db.query("SELECT payload, dedupe_key FROM tasks WHERE kind = 'reverify_chunk'")


@pytest.fixture
def private_upload(user_headers, f):
    """User A's private upload, with cached page text another user must not read."""
    job_id = f.make_job(url=SECRET_URL, source="upload", uploaded_by=_uid(user_headers))
    f.make_fetch(SECRET_URL, content=SECRET_TEXT)
    return job_id


@pytest.fixture
def shared_posting(user_headers, other_user_headers, f):
    """A catalog posting the fleet verified open, on both people's boards."""
    job_id, url = f.make_ready_job()
    f.make_board_row(_uid(user_headers), job_id)
    f.make_board_row(_uid(other_user_headers), job_id)
    return job_id, url


@pytest.fixture
def caller_model(monkeypatch):
    """The caller's own model settings, answering whatever the test says."""
    cfg = ai.AIConfig("openai", "personal-key", "user", "gpt-5-nano")
    monkeypatch.setattr(ai_access, "require_config", lambda user: cfg)

    async def fresh(*args, **kwargs):
        return Page(1, "a posting body long enough to check " * 5), None

    monkeypatch.setattr(verdicts, "refresh_page", fresh)
    prompts: list[str] = []

    async def parse(cfg, instructions, input_text, response_model):
        prompts.append(input_text)
        if response_model.__name__ == "JobClosedResponse":
            parsed = response_model(is_closed=True, reason="says it is closed")
        else:
            parsed = response_model(answers=[])
        return parsed, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}

    monkeypatch.setattr(ai, "parse", parse)
    return prompts


def test_explain_by_another_user_queues_a_recheck_instead_of_closing(
    client, other_user_headers, shared_posting, caller_model
):
    """Another person's model saying 'closed' must not close the posting on
    the owner's board; it asks the fleet to look again, once a day."""
    job_id, url = shared_posting
    for _ in range(2):
        resp = client.post(
            f"/v1/user/jobs/{job_id}/explain", json={"check": "closed"}, headers=other_user_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "rejected"

    assert _shared_closed(url) == "passed", "the caller's run decided the shared verdict"
    explained = db.query(
        "SELECT status FROM ai_queries WHERE url = %s AND check_type = %s", (url, "explain:closed")
    )
    assert [r["status"] for r in explained] == ["rejected", "rejected"]
    rechecks = _rechecks()
    assert len(rechecks) == 1, rechecks
    assert rechecks[0]["payload"]["rows"][0]["url"] == url
    assert rechecks[0]["payload"]["force"] is True


def test_report_on_another_users_private_upload_is_404(client, other_user_headers, private_upload):
    resp = client.post(
        f"/v1/user/jobs/{private_upload}/report",
        json={"kind": "closed"},
        headers=other_user_headers,
    )
    assert resp.status_code == 404, resp.text
    assert db.query("SELECT id FROM reports") == []
    assert _rechecks() == []


def test_closed_report_queues_one_recheck_per_day(client, other_user_headers, shared_posting):
    job_id, url = shared_posting
    for kind in ("closed", "closed", "stale"):
        resp = client.post(
            f"/v1/user/jobs/{job_id}/report", json={"kind": kind}, headers=other_user_headers
        )
        assert resp.status_code == 200, resp.text
    assert _shared_closed(url) == "passed"
    assert [r["payload"]["rows"][0]["url"] for r in _rechecks()] == [url]


def test_suggest_on_another_users_private_upload_is_404(
    client, other_user_headers, private_upload, caller_model
):
    resp = client.post(
        "/v1/user/apply/suggest",
        json={
            "job_id": private_upload,
            "fields": [{"key": "why", "label": "Why this role?", "kind": "long"}],
        },
        headers=other_user_headers,
    )
    assert resp.status_code == 404, resp.text
    assert not any(SECRET_TEXT in p for p in caller_model), "the upload's text reached a prompt"


def test_resolve_does_not_match_another_users_private_upload(
    client, other_user_headers, private_upload
):
    resp = client.post(
        "/v1/user/apply/resolve",
        json={"url": SECRET_URL, "fields": [{"key": "name", "label": "Name", "kind": "text"}]},
        headers=other_user_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"] is None
    fill = db.query_one(
        "SELECT job_id FROM application_fills WHERE id = %s", (resp.json()["fill_id"],)
    )
    assert fill == {"job_id": None}


def test_submitting_a_fill_naming_another_users_upload_writes_no_board_row(
    client, other_user_headers, private_upload
):
    """A fill attached before resolve matched only touchable postings."""
    other = _uid(other_user_headers)
    fill = db.query_one(
        "INSERT INTO application_fills (user_id, job_id, url, host, fields) "
        "VALUES (%s, %s, %s, 'private.test', '[]'::jsonb) RETURNING id",
        (other, private_upload, SECRET_URL),
    )
    assert fill is not None
    resp = client.post(
        f"/v1/user/apply/fills/{fill['id']}/submitted",
        json={"fields": []},
        headers=other_user_headers,
    )
    assert resp.status_code == 200, resp.text
    assert db.query("SELECT 1 AS x FROM user_jobs WHERE user_id = %s", (other,)) == []
