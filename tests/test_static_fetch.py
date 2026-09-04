"""A browserless fetch serves a page only when it plainly came back whole;
otherwise the browser does, exactly as before. The engine is persisted
config, so the share each one serves can be read off the content rows."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api import db, fetching, verdicts
from core import ats


def _resp(text: str, status: int = 200, ctype: str = "text/html; charset=utf-8", url: str = ""):
    return SimpleNamespace(
        text=text, status_code=status, headers={"content-type": ctype}, url=url or None
    )


PAGE = "<html><body><h1>Software Engineer</h1>" + "<p>We build things.</p>" * 200 + "</body></html>"
SHELL = "<html><body><div id='root'></div><script src='app.js'></script></body></html>"


@pytest.fixture
def cffi(monkeypatch):
    import curl_cffi.requests as cffi_requests

    box: dict = {}

    def get(url, **kw):
        box["called"] = box.get("called", 0) + 1
        return box["resp"]

    monkeypatch.setattr(cffi_requests, "get", get)
    monkeypatch.setattr("api.ssrf.public_url_error", lambda url: None)
    return box


@pytest.mark.asyncio
async def test_a_whole_page_is_accepted_and_a_shell_is_not(cffi):
    cffi["resp"] = _resp(PAGE)
    text = await fetching.fetch_static("https://jobs.example.com/1", 1500)
    assert text and text.startswith("Software Engineer") and "We build things." in text
    cffi["resp"] = _resp(SHELL)
    assert await fetching.fetch_static("https://jobs.example.com/2", 1500) is None
    cffi["resp"] = _resp(PAGE, status=403)
    assert await fetching.fetch_static("https://jobs.example.com/3", 1500) is None
    cffi["resp"] = _resp('{"jobs": []}', ctype="application/json")
    assert await fetching.fetch_static("https://jobs.example.com/4", 1500) is None
    cffi["resp"] = _resp(
        "<html><body>" + "Just a moment... checking your browser " * 20 + "</body></html>"
    )
    assert await fetching.fetch_static("https://jobs.example.com/5", 1500) is None


@pytest.mark.asyncio
async def test_refresh_content_serves_static_first_and_falls_back_to_the_browser(cffi, monkeypatch):
    fetched: list[str] = []

    async def browser(url):
        fetched.append(url)
        return "browser text " * 300, False

    monkeypatch.setattr(fetching, "fetch_page", browser)
    monkeypatch.setattr(ats, "resolve", lambda url: ats.UNSUPPORTED)

    cffi["resp"] = _resp(PAGE)
    content, closure = await verdicts.refresh_content("https://jobs.example.com/a")
    assert content and content.startswith("Software Engineer") and closure is None
    assert fetched == []
    row = db.query_one(
        "SELECT reason FROM ai_queries WHERE url = 'https://jobs.example.com/a' "
        "AND check_type = 'content' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None and row["reason"] == "static"

    cffi["resp"] = _resp(SHELL)
    content, _ = await verdicts.refresh_content("https://jobs.example.com/b")
    assert (
        content and content.startswith("browser text") and fetched == ["https://jobs.example.com/b"]
    )
    row = db.query_one(
        "SELECT reason FROM ai_queries WHERE url = 'https://jobs.example.com/b' "
        "AND check_type = 'content' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None and row["reason"] == "scraped"


@pytest.mark.asyncio
async def test_the_engine_is_config_and_chromium_skips_the_static_tier(
    cffi, monkeypatch, client, admin_headers
):
    fetched: list[str] = []

    async def browser(url):
        fetched.append(url)
        return "browser text " * 300, False

    monkeypatch.setattr(fetching, "fetch_page", browser)
    monkeypatch.setattr(ats, "resolve", lambda url: ats.UNSUPPORTED)
    r = client.put(
        "/v1/admin/config/fetch_engine", json={"value": "chromium"}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    cffi["resp"] = _resp(PAGE)
    await verdicts.refresh_content("https://jobs.example.com/c")
    assert fetched == ["https://jobs.example.com/c"] and cffi.get("called", 0) == 0

    bad = client.put(
        "/v1/admin/config/fetch_engine", json={"value": "lightpanda"}, headers=admin_headers
    )
    assert bad.status_code == 400 and "chromium, static_first" in bad.json()["detail"]["message"]
    assert (
        client.put(
            "/v1/admin/config/static_fetch_min_chars", json={"value": 2000}, headers=admin_headers
        ).status_code
        == 200
    )
