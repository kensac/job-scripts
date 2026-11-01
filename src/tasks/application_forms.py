"""The questions on an application form, read under the host's budget and
cached per url in application_forms."""

from __future__ import annotations

from typing import Any

from api import db, hosts
from core.fetching import forms
from core.fetching.hosts import pace_key
from tasks.runtime import Deferred


def store_form(url: str, questions: list[forms.Question] | None, error: str | None = None) -> None:
    """One row per url: the questions as read, NULL for a host that cannot be
    read, and the error text when the read failed (questions kept as they
    were, so a transient failure does not blank a form already read)."""
    if error is not None:
        db.execute(
            "INSERT INTO application_forms (url, questions, error, fetched_at) "
            "VALUES (%s, NULL, %s, now()) ON CONFLICT (url) DO UPDATE "
            "SET error = EXCLUDED.error, fetched_at = now()",
            (url, error),
        )
        return
    payload = None if questions is None else [q.as_dict() for q in questions]
    db.execute(
        "INSERT INTO application_forms (url, questions, error, fetched_at) "
        "VALUES (%s, %s, NULL, now()) ON CONFLICT (url) DO UPDATE "
        "SET questions = EXCLUDED.questions, error = NULL, fetched_at = now()",
        (url, db.jsonb(payload) if payload is not None else None),
    )


def read_form(url: str) -> list[dict[str, Any]] | None:
    """Read the form under the host's budget and store it. Returns the
    questions, or None for a host that cannot be read. Raises Deferred when
    the host's slot is closed or the host refused, and re-raises a failed
    read after recording it."""
    if not forms.supported(url):
        store_form(url, None)
        return None
    host = pace_key(url)
    opens = hosts.take(host)
    if opens:
        raise Deferred(opens)
    try:
        questions = forms.fetch(url)
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            hosts.refused(host)
            raise Deferred(hosts.soon()) from exc
        store_form(url, None, error=str(exc))
        raise
    hosts.succeeded(host)
    store_form(url, questions or [])
    return [q.as_dict() for q in questions or []]


def sync_form_questions(url: str, *, refresh: bool = False) -> list[dict[str, Any]] | None:
    """The form's questions for this url, read once and cached. None when the
    host cannot be read. Raises Deferred when the host's slot is closed."""
    if not refresh:
        row = db.query_one("SELECT questions, error FROM application_forms WHERE url = %s", (url,))
        if row and row["error"] is None:
            return row["questions"]
    return read_form(url)
