"""The Anthropic branch of the model call runs, through the real library.

ANTHROPIC_API_KEY is set in production and Anthropic's models are offered, and
ai_queries held zero Anthropic calls across 241k rows on 2026-09-08. A
library bump on a path nobody travels breaks silently, and surfaces as the
first person's failed call rather than at roll time. So this runs api.ai's
Anthropic branch through the installed anthropic client against a recorded
Messages API response served by an httpx mock transport: a renamed request
parameter, a moved response attribute or a changed default in the library
fails here, where a hand-rolled stub standing in for the client would keep
passing. (It already did once: anthropic 1.4.0 takes its transport from
httpx2, not httpx, which the first run of this test found.)
"""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from api import ai

RECORDED = {
    "id": "msg_recorded",
    "type": "message",
    "role": "assistant",
    "model": "claude-fable-5-1",
    "content": [{"type": "text", "text": json.dumps({"verdict": "keep", "reason": "fits"})}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 120,
        "output_tokens": 9,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 0,
    },
}


class Verdict(BaseModel):
    verdict: str
    reason: str


@pytest.mark.asyncio
async def test_the_anthropic_branch_round_trips_a_recorded_response(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=RECORDED)

    # The real client, with its network replaced; everything the library does
    # to build the request and read the response still runs.
    def client_with_mock_transport(**kwargs):
        return AsyncAnthropic(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )

    monkeypatch.setattr(ai, "AsyncAnthropic", client_with_mock_transport)
    cfg = ai.AIConfig(
        provider="anthropic",
        api_key="test-key",
        key_source="owner",
        model="claude-fable-5-1",
        params={"effort": "high", "max_output_tokens": 500},
    )
    parsed, usage = await ai._parse(cfg, "Judge the posting.", "A posting.", Verdict)

    assert parsed == Verdict(verdict="keep", reason="fits")
    assert usage["prompt_tokens"] == 120 and usage["completion_tokens"] == 9
    assert usage["cached_tokens"] == 40
    assert seen["headers"]["x-api-key"] == "test-key"
    assert seen["url"].endswith("/v1/messages")
    body = seen["body"]
    assert body["model"] == "claude-fable-5-1" and body["max_tokens"] == 500
    assert body["system"] == "Judge the posting."
    assert body["messages"] == [{"role": "user", "content": "A posting."}]
    assert body["output_config"]["effort"] == "high"
    # Structured output rides on the request in whatever form the installed
    # library uses; a schema for the model must be somewhere in it.
    assert "verdict" in json.dumps(body)
