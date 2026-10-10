"""core.fetching.client: the one HTTP client every fetcher speaks through,
against a real local server so the transport retry is the real one."""

from __future__ import annotations

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

        def log_message(self, *args):
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
