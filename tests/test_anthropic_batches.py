"""The Anthropic batch transport through the installed client library.

Recorded Message Batches responses go through an httpx mock transport, so
everything the library does to build requests and read the streamed results
still runs: the request a batch carries, the id a posting url is sent under,
and the receipt every consumer reads.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx2 as httpx
import pytest
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from core import batch, pricing
from core.batch import anthropic_batches

URL = "https://boards.greenhouse.io/acme/jobs/1234567?gh_src=a-very-long-tracking-parameter"
MODEL = "claude-haiku-5-5"
BATCH_ID = "msgbatch_01recorded"


class Verdict(BaseModel):
    keep: bool
    reason: str


def _batch(status: str) -> dict:
    return {
        "id": BATCH_ID,
        "type": "message_batch",
        "processing_status": status,
        "request_counts": {
            "processing": 0 if status == "ended" else 2,
            "succeeded": 1 if status == "ended" else 0,
            "errored": 1 if status == "ended" else 0,
            "canceled": 0,
            "expired": 0,
        },
        "created_at": "2026-10-30T00:00:00Z",
        "expires_at": "2026-10-31T00:00:00Z",
        "ended_at": "2026-10-30T00:10:00Z" if status == "ended" else None,
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": (
            f"https://api.anthropic.com/v1/messages/batches/{BATCH_ID}/results"
            if status == "ended"
            else None
        ),
    }


def _results() -> str:
    succeeded = {
        "custom_id": anthropic_batches.wire_id(URL),
        "result": {
            "type": "succeeded",
            "message": {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": MODEL,
                "content": [{"type": "text", "text": '{"keep": true, "reason": "fits"}'}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 1200,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    }
    errored = {
        "custom_id": "plain_id",
        "result": {
            "type": "errored",
            "error": {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "bad schema"},
                "request_id": None,
            },
        },
    }
    return "\n".join(json.dumps(line) for line in (succeeded, errored)) + "\n"


@pytest.fixture
def vendor(monkeypatch):
    seen: dict = {"status": "in_progress"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1/messages/batches":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=_batch("in_progress"))
        if path.endswith("/results"):
            return httpx.Response(200, text=_results())
        return httpx.Response(200, json=_batch(seen["status"]))

    client = AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(anthropic_batches, "_batch_client", client)
    monkeypatch.setattr(batch, "_client", lambda: pytest.fail("reached the OpenAI client"))
    monkeypatch.setattr(anthropic_batches.store, "record_batch_errors", lambda *a: None)
    # The submitting task's frozen requests (store.submitted_custom_ids).
    monkeypatch.setattr(batch.store, "submitted_custom_ids", lambda _: [URL, "plain_id"])
    return seen


def _specs() -> list[batch.BatchSpec]:
    return [
        batch.structured_response_spec(URL, "Judge the posting.", "posting text", Verdict),
        batch.structured_response_spec(
            "plain_id",
            "Judge the posting.",
            "other text",
            Verdict,
            context={"prompt_cache_policy": "no_cache"},
        ),
    ]


@pytest.mark.asyncio
async def test_a_batch_round_trips_under_the_callers_ids(vendor):
    ids = await batch.submit_batches(_specs(), MODEL, "low", 512)
    assert ids == [BATCH_ID]

    sent = vendor["body"]["requests"]
    assert [r["custom_id"] for r in sent] == [anthropic_batches.wire_id(URL), "plain_id"]
    assert len(sent[0]["custom_id"]) <= 64
    first = sent[0]["params"]
    assert first["model"] == MODEL
    assert first["max_tokens"] == 512
    assert first["output_config"]["effort"] == "low"
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert first["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in sent[1]["params"]["system"][0]

    progress = await batch.batch_progress(ids)
    assert progress[BATCH_ID].status == "in_progress"
    results, unfinished = await batch.collect_finished_batches(ids)
    assert results == [] and unfinished == ids

    vendor["status"] = "ended"
    progress = await batch.batch_progress(ids)
    assert (progress[BATCH_ID].status, progress[BATCH_ID].completed) == ("completed", 1)
    results, unfinished = await batch.collect_finished_batches(ids)
    assert unfinished == []
    by_id = {r.custom_id: r for r in results}
    assert Verdict.model_validate_json(by_id[URL].text or "").keep is True
    assert by_id[URL].batch_id == BATCH_ID
    assert by_id["plain_id"].error == "bad schema"

    # Cache reads are counted inside the prompt, as an OpenAI receipt counts
    # them, and price at the cached rate.
    usage = by_id[URL].usage or {}
    assert usage["input_tokens"] == 2200
    assert usage["input_tokens_details"] == {"cached_tokens": 1200, "cache_write_tokens": 0}
    cost = pricing.estimate_cost_usd(
        MODEL, 2200, 100, cached_tokens=1200, cache_write_tokens=0, batched=True
    )
    assert cost == (1000 * Decimal("0.10") + 1200 * Decimal("0.01") + 100 * Decimal("0.50")) / (
        2 * 10**6
    )


@pytest.mark.asyncio
async def test_an_effort_the_model_refuses_sends_nothing(vendor):
    with pytest.raises(ValueError, match="effort"):
        await batch.submit_batches(_specs(), MODEL, "minimal", 512)
    assert "body" not in vendor


def test_a_cache_write_is_priced_at_the_one_hour_rate():
    cost = pricing.estimate_cost_usd(MODEL, 1000, 0, cache_write_tokens=1000)
    assert cost == 1000 * Decimal("0.20") / 10**6
