"""Making a stranger's HTML safe to show a person.

The inputs here are the ones an attacker or a marketing platform actually
sends, not tidy fixtures - this is the one surface in the product where being
wrong is not recoverable by a later change.
"""

from __future__ import annotations

import pytest

from core.mail_html import sanitise


@pytest.mark.parametrize(
    "payload",
    [
        "<script>steal()</script>",
        "<p onclick='steal()'>click</p>",
        "<img src=x onerror='steal()'>",
        "<a href='javascript:steal()'>link</a>",
        "<iframe src='https://evil.test'></iframe>",
        "<object data='https://evil.test'></object>",
        "<embed src='https://evil.test'>",
        "<form action='https://evil.test'><input name='p'></form>",
        "<svg><script>steal()</script></svg>",
        '<a href="jAvAsCrIpT:steal()">mixed case</a>',
    ],
)
def test_nothing_that_executes_survives(payload):
    safe, _ = sanitise(payload)
    lowered = safe.lower()
    assert "script" not in lowered
    assert "onerror" not in lowered and "onclick" not in lowered
    assert "javascript:" not in lowered
    assert "<iframe" not in lowered and "<object" not in lowered and "<embed" not in lowered
    assert "<form" not in lowered and "<input" not in lowered


def test_a_remote_image_does_not_load_on_open():
    """Every remote image in recruiter mail is a read receipt. Rendering one
    tells the sender the moment he opened it, and no later change takes that
    back."""
    safe, blocked = sanitise('<p>hi<img src="https://tracker.test/px.gif?u=42"></p>')
    assert blocked == 1
    assert "src=" not in safe.replace("data-blocked-src=", "")
    # Kept, not deleted, so a reader can offer to load it and the choice stays
    # the person's.
    assert "data-blocked-src" in safe
    assert "tracker.test/px.gif?u=42" in safe


def test_a_protocol_relative_source_is_remote_too():
    safe, blocked = sanitise('<img src="//tracker.test/px.gif">')
    assert blocked == 1
    assert "data-blocked-src" in safe


def test_srcset_is_blocked_as_well_as_src():
    """A responsive image fetches through srcset even with no src at all."""
    safe, blocked = sanitise('<img srcset="https://tracker.test/px.gif 1x">')
    assert blocked == 1
    assert "data-blocked-srcset" in safe


@pytest.mark.parametrize("attr", ["background", "poster"])
def test_other_fetching_attributes_do_not_survive_at_all(attr):
    """These live on tags or attributes the allowlist does not keep, so they
    are dropped outright rather than deferred - safer, and the assertion is
    that nothing fetches, not that it was moved."""
    safe, _ = sanitise(f'<td {attr}="https://tracker.test/px.gif">x</td>')
    assert attr not in safe.lower()
    assert "tracker.test" not in safe


def test_mail_css_cannot_escape_into_the_app():
    """Mail CSS is written assuming it owns the document. A position:fixed rule
    in a template would lay itself over the application."""
    safe, _ = sanitise(
        "<style>body{display:none}</style>"
        "<p style='position:fixed;top:0;left:0;width:100vw'>over everything</p>"
    )
    assert "<style" not in safe.lower()
    assert "position:fixed" not in safe.replace(" ", "")
    assert "over everything" in safe


def test_a_link_keeps_its_destination():
    """A link is not a fetch - it goes nowhere until the person clicks. An
    "apply here" that leads nowhere is a broken message, not a safe one."""
    safe, blocked = sanitise('<a href="https://jobs.example.test/apply?id=7">apply here</a>')
    assert blocked == 0
    assert "https://jobs.example.test/apply?id=7" in safe
    assert "apply here" in safe


def test_the_readable_content_survives():
    """A sanitiser that eats the message is not safe, it is broken."""
    safe, blocked = sanitise(
        "<div><h2>Interview scheduled</h2><p>Hi <b>Kanishk</b>, we would like to "
        "meet on <i>Tuesday</i>.</p>"
        "<table><tr><td>Time</td><td>2pm</td></tr></table>"
        '<a href="mailto:careers@example.test">reply</a></div>'
    )
    assert blocked == 0
    for fragment in ("Interview scheduled", "Kanishk", "Tuesday", "2pm", "reply"):
        assert fragment in safe
    assert "<table" in safe and "<td" in safe, "a table must still read as a table"
    assert "mailto:careers@example.test" in safe


def test_a_link_carries_no_opener_to_the_page_it_opens():
    safe, _ = sanitise('<a href="mailto:x@y.test">m</a>')
    assert "noopener" in safe and "noreferrer" in safe


def test_an_inline_cid_image_is_not_treated_as_remote():
    """An attached image travels with the message; loading it phones nobody."""
    safe, blocked = sanitise('<img src="cid:logo@example">')
    assert blocked == 0
    assert "cid:logo@example" in safe


def test_empty_and_garbage_inputs_do_not_raise():
    for payload in ("", "   ", "not html at all", "<p>unclosed", "<<<>>>"):
        safe, blocked = sanitise(payload)
        assert isinstance(safe, str) and blocked == 0


def test_the_reader_never_receives_the_raw_markup(client, user_headers, f):
    """Sanitised on read, and the raw is not also returned. A caller holding
    the original will eventually render it."""
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
        "subject, sent_at, body_text, body_html) VALUES "
        "(%s, 'san-1', 'olm', 'a@b.com', 's', now(), %s, %s) RETURNING id",
        (
            uid,
            "Interview scheduled.",
            '<p onclick="steal()">Interview scheduled.'
            '<img src="https://tracker.test/px.gif"><script>steal()</script></p>',
        ),
    )
    body = client.get(f"/v1/user/messages/{row['id']}", headers=user_headers).json()

    assert "Interview scheduled." in body["body_html"]
    assert "script" not in body["body_html"].lower()
    assert "onclick" not in body["body_html"].lower()
    assert body["blocked_remote_content"] == 1
    assert "data-blocked-src" in body["body_html"]
    # body_text keeps its own job untouched.
    assert body["body_text"] == "Interview scheduled."


def test_a_message_with_no_markup_reports_nothing_to_load(client, user_headers, f):
    from api import db

    uid = db.query_one("SELECT id FROM users WHERE sub = 'test-user'")["id"]
    row = db.query_one(
        "INSERT INTO email_messages (user_id, provider_message_id, source, from_email, "
        "subject, sent_at, body_text) VALUES "
        "(%s, 'san-2', 'olm', 'a@b.com', 's', now(), 'plain text only') RETURNING id",
        (uid,),
    )
    body = client.get(f"/v1/user/messages/{row['id']}", headers=user_headers).json()
    assert body["body_html"] is None
    assert body["blocked_remote_content"] == 0
