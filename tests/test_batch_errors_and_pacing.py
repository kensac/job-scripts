"""Every error a batch returns is stored; the whole-failure alert says why and
resolves on recovery; a host that blocks bursts is drip-fed.

On 2026-09-04, 49 requirements batches failed every request and nothing kept
the provider's reason; the alert then outlived the fix by the rest of the
24h window. The same day www.tesla.com blocked 19 of 32 fetches after
serving 12 in one hour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import db, health, verdicts
from core import batch
from core.store import add_ai_result

ERROR_LINE = (
    '{"custom_id": "u1", "error": {"code": "invalid_request", '
    '"message": "reasoning.effort minimal is not supported"}, "response": null}'
)


class _Client:
    def __init__(self, text: str):
        self.files = SimpleNamespace(content=self._content)
        self._text = text

    async def _content(self, file_id: str):
        return SimpleNamespace(text=self._text)


def _errors(batch_id: str) -> list[dict]:
    return db.query(
        "SELECT custom_id, error FROM ai_batch_errors WHERE provider_batch_id = %s ORDER BY id",
        (batch_id,),
    )


@pytest.mark.asyncio
async def test_per_request_errors_are_stored_as_the_provider_wrote_them():
    fake = SimpleNamespace(
        id="b-req", status="completed", output_file_id=None, error_file_id="e1", errors=None
    )
    results = {"u1": batch.BatchResult("u1")}
    await batch._collect_batch(_Client(ERROR_LINE), fake, results)  # type: ignore[arg-type]
    assert results["u1"].error and "minimal is not supported" in results["u1"].error
    stored = _errors("b-req")
    assert len(stored) == 1 and stored[0]["custom_id"] == "u1"
    assert "minimal is not supported" in stored[0]["error"]


@pytest.mark.asyncio
async def test_a_batch_rejected_before_any_request_ran_keeps_its_reason():
    """No output file, no error file: the reason is on the batch object and
    would otherwise vanish with the collection."""
    fake = SimpleNamespace(
        id="b-whole",
        status="failed",
        output_file_id=None,
        error_file_id=None,
        errors=SimpleNamespace(data=[SimpleNamespace(message="model not eligible for batch")]),
    )
    results = {"u1": batch.BatchResult("u1"), "u2": batch.BatchResult("u2")}
    await batch._collect_batch(_Client(""), fake, results)  # type: ignore[arg-type]
    assert all("model not eligible" in (r.error or "") for r in results.values())
    assert {e["custom_id"] for e in _errors("b-whole")} == {"u1", "u2"}


def _batch(purpose: str, batch_id: str, requests: int, failed: int, minutes_ago: int) -> None:
    db.execute(
        "INSERT INTO ai_batches (provider_batch_id, purpose, model, requests, completed, "
        "failed_count, status, submitted_at) VALUES (%s, %s, 'gpt-5-nano', %s, %s, %s, "
        "'completed', now() - make_interval(mins => %s))",
        (batch_id, purpose, requests, requests - failed, failed, minutes_ago),
    )


def _alerts() -> list[dict]:
    return [a for a in health.detect() if a["kind"] == "batch_failed_whole"]


def test_the_alert_names_the_stored_reason_and_resolves_once_the_purpose_recovers():
    _batch("requirements", "b1", 400, 400, 180)
    _batch("requirements", "b2", 400, 400, 120)
    db.execute(
        "INSERT INTO ai_batch_errors (provider_batch_id, custom_id, error) VALUES "
        "('b1', 'u1', 'reasoning.effort minimal is not supported'), "
        "('b2', 'u2', 'reasoning.effort minimal is not supported'), "
        "('b2', 'u3', 'something rarer')"
    )
    (alert,) = _alerts()
    assert alert["subject"] == "requirements"
    assert "minimal is not supported" in alert["message"]
    assert alert["detail"]["reason"] == "reasoning.effort minimal is not supported"

    # A later batch for the same purpose that survived means the cause is
    # fixed, whatever fixed it: the alert stops, inside the window.
    _batch("requirements", "b3", 300, 0, 30)
    assert _alerts() == []

    # A later whole failure reopens it; a partial failure is not a recovery
    # signal for a different purpose.
    _batch("requirements", "b4", 200, 200, 10)
    _batch("mail_classify", "b5", 50, 50, 5)
    assert {a["subject"] for a in _alerts()} == {"requirements", "mail_classify"}


def test_a_paced_host_defers_once_its_hour_is_used(client, admin_headers):
    r = client.put(
        "/v1/admin/config/fetch_host_limits",
        json={"value": {"WWW.Tesla.com ": 2}},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["value"] == {"www.tesla.com": 2}
    assert verdicts.host_paced("https://www.tesla.com/careers/1") is False
    add_ai_result("https://www.tesla.com/careers/1", "passed", "scraped", "content")
    add_ai_result("https://www.tesla.com/careers/2", "failed", "fetch returned nothing", "content")
    assert verdicts.host_paced("https://www.tesla.com/careers/3") is True
    # Another host is not paced, and a host absent from the map never is.
    assert verdicts.host_paced("https://jobs.example.com/1") is False

    bad = client.put(
        "/v1/admin/config/fetch_host_limits",
        json={"value": {"www.tesla.com": 0}},
        headers=admin_headers,
    )
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "INVALID_VALUE"


@pytest.mark.asyncio
async def test_a_deferred_fetch_writes_nothing_so_the_next_cycle_retries(monkeypatch):
    from api import fetching
    from core import ats

    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('fetch_host_limits', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb({"slow.example.com": 1}),),
    )
    add_ai_result("https://slow.example.com/1", "passed", "scraped", "content")
    fetched: list[str] = []

    async def fetch_page(url):
        fetched.append(url)
        return "page", False

    monkeypatch.setattr(fetching, "fetch_page", fetch_page)
    monkeypatch.setattr(ats, "resolve", lambda url: ats.UNSUPPORTED)
    content, closure = await verdicts.refresh_content("https://slow.example.com/2")
    assert (content, closure) == (None, None) and fetched == []
    assert (
        db.query_one(
            "SELECT count(*) AS n FROM ai_queries WHERE url = 'https://slow.example.com/2'"
        )["n"]
        == 0
    )
