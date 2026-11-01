"""Scraped text reaches mail HTML as text, never as markup."""

from __future__ import annotations

from api import mail

_sent: list = []


class _Smtp:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, *args):
        pass

    def send_message(self, msg):
        _sent.append(msg)


def _html(monkeypatch, send):
    _sent.clear()
    monkeypatch.setattr(mail.smtplib, "SMTP", _Smtp)
    send()
    (msg,) = _sent
    return msg.get_body(("html",)).get_content()


def test_a_digest_escapes_scraped_company_and_title(monkeypatch):
    job = {"company": "Acme <b>", "title": '<a href="https://x">Apply</a>', "locations": []}
    body = _html(monkeypatch, lambda: mail.send_digest("p@example.com", [job], "t"))
    assert '<a href="https://x">' not in body
    assert "&lt;a href=&quot;https://x&quot;&gt;" in body
    assert "Acme &lt;b&gt;" in body


def test_a_health_alert_escapes_its_message(monkeypatch):
    alert = {"severity": "warn", "message": "source <img src=x> stopped"}
    body = _html(monkeypatch, lambda: mail.send_health_alert("a@example.com", [alert]))
    assert "<img" not in body and "&lt;img src=x&gt;" in body
