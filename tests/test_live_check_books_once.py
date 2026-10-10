"""A live check is booked to the call ledger once, by run_check, whatever
path asked it: the admin re-check, a person's explain (test_live_usage_ledger)
and a live filter run. Its caller books nothing more."""

from __future__ import annotations

from decimal import Decimal

import pytest

from api import ai, db
from api.ai import verdicts
from api.model_calls import Payer
from core import pricing
from core.checks import JobClosedResponse
from core.store import Page
from tasks import filter_execution

USAGE = {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}


def _calls() -> list[dict]:
    return db.query("SELECT id, purpose, user_id, total_tokens, cost_usd FROM model_calls")


@pytest.mark.parametrize("paid_error", [False, True])
def test_an_admin_recheck_is_one_call_and_spend_counts_it_once(
    client, admin_headers, monkeypatch, paid_error
):
    db.execute(
        "INSERT INTO jobs (url, source, company, title) VALUES ('https://once.test/1','s','C','T')"
    )
    jid = db.query_one("SELECT id FROM jobs WHERE url = 'https://once.test/1'")["id"]
    monkeypatch.setattr("core.routing.server_key", lambda provider: "sk-test")

    async def fresh(url, **kw):
        return Page(1, "A posting body long enough to check."), None

    async def parse(cfg, instructions, input_text, response_model):
        if paid_error:
            raise ai.PaidParseError("invalid output", USAGE)
        return JobClosedResponse(is_closed=False, reason="open"), USAGE

    monkeypatch.setattr(verdicts, "refresh_page", fresh)
    monkeypatch.setattr(ai, "parse", parse)

    if paid_error:
        with pytest.raises(ai.PaidParseError):
            client.post(
                "/v1/admin/checks/run",
                json={"job_id": jid, "check": "closed"},
                headers=admin_headers,
            )
    else:
        r = client.post(
            "/v1/admin/checks/run", json={"job_id": jid, "check": "closed"}, headers=admin_headers
        )
        assert r.status_code == 200, r.text

    [call] = _calls()
    assert call["purpose"] == "manual" and call["total_tokens"] == 1100
    verdict = db.query_one("SELECT model_call_id FROM ai_queries WHERE url = 'https://once.test/1'")
    assert verdict["model_call_id"] == call["id"]
    body = client.get("/v1/admin/spend?days=30", headers=admin_headers).json()
    assert Decimal(str(body["ledger"]["totals"]["cost_usd"])) == call["cost_usd"]
    assert body["ledger"]["totals"]["calls"] == 1


@pytest.mark.asyncio
async def test_a_live_filter_check_is_one_call(f, monkeypatch):
    payer, purpose = Payer(user_id=f.make_user()), "filter"
    url = "https://once.test/filter"
    fetch = f.make_fetch(url, content="a posting body " * 30)

    async def parse(cfg, instructions, input_text, response_model):
        return response_model(should_filter=False), USAGE

    monkeypatch.setattr(ai, "parse", parse)
    usage_hook = []
    hooks = filter_execution.ExecutionHooks(
        verdict_label="once",
        key_source="owner",
        payer=payer,
        purpose=purpose,
        record_usage=lambda usage, model, batched: usage_hook.append(batched),
        budget_exceeded=lambda: False,
        cancelled=lambda: False,
        progress=lambda *_: None,
        complete=lambda: None,
    )
    await filter_execution.execute_live(
        f.make_task("run_filter_chunk"),
        ai.AIConfig("openai", "key", "owner", "gpt-5-nano"),
        filter_execution.FilterSnapshot("once", "prompt", "keep", "once-hash"),
        [{"url": url, "company": "C", "title": "T"}],
        hooks,
    )

    [call] = _calls()
    assert call["purpose"] == purpose and call["total_tokens"] == 1100
    assert call["cost_usd"] == pricing.estimate_cost_usd("gpt-5-nano", 1000, 100)
    assert usage_hook == [], "the caller books nothing for a live call"
    answer = db.query_one("SELECT model_call_id, page_fetch_id FROM ai_queries")
    assert answer == {"model_call_id": call["id"], "page_fetch_id": fetch}
