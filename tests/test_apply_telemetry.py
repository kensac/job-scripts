"""What the extension does reaches PostHog, keyed to the same person as the web.

Two things are asserted here rather than assumed. The distinct id is the IdP
subject, because auth.js mints a fresh token.sub at every sign-in and surfaces
idp_sub to PostHog instead - anything else puts the extension's funnel on a
different person from that person's own web session. And the properties carry
counts and a host, never a question or an answer: the content of an application
is the person's, the shape of it is what says whether a reader works.
"""

from __future__ import annotations

import pytest

from api import telemetry


@pytest.fixture
def captured(monkeypatch):
    """Every telemetry.capture the request makes, as (event, distinct_id, props)."""
    seen: list[tuple[str, str, dict]] = []

    def fake(event, distinct_id=telemetry.SERVICE, properties=None):
        seen.append((event, distinct_id, properties or {}))

    monkeypatch.setattr(telemetry, "capture", fake)
    return seen


def _sub(headers: dict) -> str:
    return headers["X-User-Sub"]


def _resolve(client, headers, url="https://boards.greenhouse.io/acme/jobs/1"):
    return client.post(
        "/v1/user/apply/resolve",
        json={
            "url": url,
            "fields": [
                {"key": "first_name", "label": "First name", "kind": "text"},
                {"key": "why_us", "label": "Why do you want to work here?", "kind": "textarea"},
            ],
        },
        headers=headers,
    )


def test_resolving_a_form_is_one_event_on_the_idp_subject(client, user_headers, captured):
    resp = _resolve(client, user_headers)
    assert resp.status_code == 200, resp.text

    events = [e for e in captured if e[0] == "apply_form_resolved"]
    assert len(events) == 1, captured
    _event, distinct_id, props = events[0]

    assert distinct_id == _sub(user_headers), (
        "the extension's events must land on the same person as the web session"
    )
    assert props["host"] == "boards.greenhouse.io"
    assert props["fields"] == 2
    assert props["matched_job"] is False


def test_the_event_carries_no_question_and_no_answer(client, user_headers, captured):
    """A label is what a form asked this person; a value is what they said."""
    _resolve(client, user_headers)

    (_e, _d, props) = next(e for e in captured if e[0] == "apply_form_resolved")
    blob = repr(props).lower()
    for leak in ("why do you want", "first name", "why_us"):
        assert leak not in blob, f"{leak!r} is application content and must not ship: {props}"


def test_a_reported_page_names_its_host_and_not_the_note(client, user_headers, captured):
    """The report is the one signal that a reader failed on a real page, so the
    host ships. The note is the person's own words and does not."""
    resp = client.post(
        "/v1/user/apply/reports",
        json={
            "url": "https://jobs.lever.co/acme/abc",
            "note": "the salary box never fills",
            "page": {"fields": []},
        },
        headers=user_headers,
    )
    assert resp.status_code == 201, resp.text

    (_e, distinct_id, props) = next(e for e in captured if e[0] == "apply_page_reported")
    assert distinct_id == _sub(user_headers)
    assert props["host"] == "jobs.lever.co"
    assert props["has_note"] is True
    assert "salary box" not in repr(props), props


def test_posthog_being_down_does_not_fail_the_autofill(client, user_headers, monkeypatch):
    """A telemetry failure must not become the second failure of the thing it
    was recording - here, the person's autofill.

    The client is what breaks, not capture(): capture already wraps its body and
    documents that it never raises, so patching capture itself would simulate a
    state that cannot occur. Patching the client underneath it exercises the
    guard that actually runs in production.
    """

    class Down:
        def capture(self, *_args, **_kwargs):
            raise RuntimeError("posthog is down")

    monkeypatch.setattr(telemetry, "_client", Down())

    assert _resolve(client, user_headers).status_code == 200
