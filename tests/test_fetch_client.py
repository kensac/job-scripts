"""core.fetching.client: the one HTTP client every fetcher speaks through,
against a real local server so the transport retry is the real one."""

from __future__ import annotations

import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from urllib3.util.retry import Retry

from core.fetching import boards, client


@pytest.fixture
def upstream(monkeypatch):
    """A host that answers each request with the next status in `answers`."""
    monkeypatch.setattr(Retry, "sleep", lambda self, response=None: None)
    answers: list[int] = []
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.headers.get("User-Agent") or "")
            status = answers.pop(0) if answers else 200
            body = b"[]"
            self.send_response(status)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/feed.json", answers, hits
    server.shutdown()


def test_a_refusal_comes_back_at_once_for_the_host_budget(upstream):
    """A 429 is the host budget's to answer for every worker on the address;
    slept on and retried here, the refused address asks again before the
    budget hears of it."""
    url, answers, hits = upstream
    answers.extend([429, 200])
    assert client.session.get(url).status_code == 429
    assert len(hits) == 1


def test_a_server_error_is_retried_and_its_status_reaches_the_caller(upstream):
    url, answers, hits = upstream
    answers.extend([503, 200])
    assert client.session.get(url).status_code == 200
    answers.extend([503] * 4)
    # Three retries, then the last answer itself rather than a RetryError
    # without a status, so the ingest failure says 503.
    assert client.session.get(url).status_code == 503
    assert len(hits) == 2 + 4
    assert hits[0] == client.USER_AGENT


def test_every_board_format_waits_out_its_hosts_floor(monkeypatch):
    """Lever never paced its own pages; a floor set for its host still holds."""
    slept = []
    monkeypatch.setattr(client.time, "sleep", slept.append)
    monkeypatch.setattr(client, "_last_call", {})

    class Empty:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    monkeypatch.setattr(boards._session, "get", lambda url, **kw: Empty())
    client.set_pace({"api.lever.co": 5})
    try:
        boards.fetch_listings("https://api.lever.co/v0/postings/a?mode=json", "A")
        boards.fetch_listings("https://api.lever.co/v0/postings/b?mode=json", "B")
    finally:
        client.set_pace({})
    assert slept and 4.0 < slept[-1] <= 5.0


def test_the_stdlib_post_hands_a_refusal_back_as_a_response(monkeypatch):
    import email.message
    import io
    import urllib.error

    import requests

    from core.fetching import client

    headers = email.message.Message()
    headers["Retry-After"] = "60"

    def refuse(request, timeout):
        assert request.get_method() == "POST" and request.data == b'{"offset": 0}'
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", headers, io.BytesIO(b"slow down")
        )

    monkeypatch.setattr(client.urllib.request, "urlopen", refuse)
    response = client.stdlib_session.post(
        "https://x.wd1.myworkdayjobs.com/wday/cxs/x/y/jobs", json={"offset": 0}
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.text == "slow down"
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        assert error.response is response
    else:
        raise AssertionError("a 429 must raise")


class _Reply(io.BytesIO):
    status, reason, headers = 200, "OK", {}


def test_a_workday_posting_read_goes_through_the_standard_library(monkeypatch):
    """The session got 429 for every Workday tenant from the fleet on
    2026-11-21 while urllib from the same address got 200."""
    from core.fetching import ats, client

    def answer(request, timeout):
        assert request.get_method() == "GET" and request.data is None
        assert request.full_url == "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Ext/job/NY/E_1"
        return _Reply(b'{"jobPostingInfo": {"title": "Engineer", "jobDescription": "x"}}')

    monkeypatch.setattr(client.urllib.request, "urlopen", answer)
    monkeypatch.setattr(client.session, "get", lambda *a, **k: pytest.fail("requests session"))
    assert ats.Workday().fetch("https://acme.wd1.myworkdayjobs.com/en-US/Ext/job/NY/E_1").ok


def test_the_stdlib_session_retries_a_5xx_and_never_a_429(monkeypatch):
    """Walmart failed every pull from 2026-11-21 on one 502 among thousands
    of sliced searches; the requests session would have retried it."""
    import email.message
    import urllib.error

    answers = [502, 503, 200]
    asked: list[int] = []

    def reply(request, timeout):
        status = answers.pop(0)
        asked.append(status)
        if status != 200:
            raise urllib.error.HTTPError(
                request.full_url, status, "err", email.message.Message(), io.BytesIO(b"")
            )
        return _Reply(b'{"total": 1}')

    monkeypatch.setattr(client.urllib.request, "urlopen", reply)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    assert (
        client.stdlib_session.post("https://x.wd1.myworkdayjobs.com/j", json={}).status_code == 200
    )
    assert asked == [502, 503, 200]

    answers[:] = [429, 200]
    asked.clear()
    assert (
        client.stdlib_session.post("https://x.wd1.myworkdayjobs.com/j", json={}).status_code == 429
    )
    assert asked == [429]
