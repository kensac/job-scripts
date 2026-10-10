from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from api import ai, db
from api.ai import access as ai_access
from api.ai import verdicts
from core import pricing
from core.checks import JobClosedResponse
from core.store import Page
from tasks import uploads


@pytest.mark.parametrize("kind", ["explain", "suggest", "improve", "upload"])
@pytest.mark.parametrize("usable", [True, False, "paid_error"])
def test_live_usage_is_recorded_once_with_cache_even_without_usable_output(
    client, user_headers, f, monkeypatch, kind, usable
):
    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    cfg = ai.AIConfig("openai", "test-key", "user", "gpt-5-nano")
    monkeypatch.setattr(ai_access, "require_config", lambda user: cfg)
    usage = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "cached_tokens": 800,
    }

    async def response(*args, **kwargs):
        if usable == "paid_error":
            raise ai.PaidParseError("paid invalid response", usage)
        if usable and kind == "explain":
            return JobClosedResponse(is_closed=False, reason="open"), usage
        parsed = (
            SimpleNamespace(
                improved="remote roles",
                rationale="clearer",
                answers=[],
                is_closed=False,
                reason="open",
                company="Example",
                title="Role",
                locations=[],
                terms=[],
            )
            if usable
            else None
        )
        return parsed, usage

    monkeypatch.setattr(ai, "parse", response)
    job_id, _url = f.make_ready_job()
    f.make_board_row(uid, job_id)
    if kind == "upload":
        monkeypatch.setattr(uploads, "load_config", lambda user_id: (None, cfg))
        if usable == "paid_error":
            with pytest.raises(ai.PaidParseError, match="paid invalid response"):
                asyncio.run(uploads.handle_extract_upload({"job_id": job_id, "user_id": uid}))
        elif usable:
            asyncio.run(uploads.handle_extract_upload({"job_id": job_id, "user_id": uid}))
        else:
            with pytest.raises(RuntimeError, match="no parsed output"):
                asyncio.run(uploads.handle_extract_upload({"job_id": job_id, "user_id": uid}))
        purpose = "extract"
    else:
        if kind == "explain":

            async def fresh(url, **kwargs):
                text = "current posting " * 20
                return Page(f.make_fetch(url, content=text), text), None

            # The real run_check, which books the call with its verdict.
            monkeypatch.setattr(verdicts, "refresh_page", fresh)
            path, body, purpose = f"/v1/user/jobs/{job_id}/explain", {"check": "closed"}, "explain"
        elif kind == "suggest":
            path, body, purpose = (
                "/v1/user/apply/suggest",
                {"fields": [{"key": "why", "label": "Why this role?", "kind": "long"}]},
                "application",
            )
        else:
            path, body, purpose = "/v1/ai/improve-prompt", {"prompt": "remote"}, "improve_prompt"
        if usable == "paid_error":
            with pytest.raises(ai.PaidParseError, match="paid invalid response"):
                client.post(path, json=body, headers=user_headers)
        else:
            result = client.post(path, json=body, headers=user_headers)
            assert result.status_code == (200 if usable else 502), result.text
    rows = db.query(
        "SELECT user_id, key_source, purpose, cached_tokens, total_tokens, batched, cost_usd FROM model_calls"
    )
    assert rows == [
        {
            "user_id": uid,
            "key_source": "user",
            "purpose": purpose,
            "cached_tokens": 800,
            "total_tokens": 1100,
            "batched": False,
            "cost_usd": pricing.estimate_cost_usd(cfg.model, 1000, 100, cached_tokens=800),
        }
    ]
    if kind == "explain":
        # The verdict names the call it booked and the page it read, and the
        # page it was asked about rebuilds from that fetch.
        [answer] = [a for a in f.answer_pointers(_url) if a["check_type"] == "explain:closed"]
        call = db.query_one("SELECT id, duration_ms FROM model_calls")
        assert answer["model_call_id"] == call["id"] and call["duration_ms"] is not None
        assert answer["page_fetch_id"] is not None
        assert answer["rebuilt"] == answer["copy"]
