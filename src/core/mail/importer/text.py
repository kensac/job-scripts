"""Normalisation shared by imported and live mail."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape

_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")
_TAG = re.compile(r"<[^>]+>")


def _strip_nul(text: str) -> str:
    """Remove NUL bytes before inserting text into PostgreSQL.

    A NUL rejects the whole insert batch. A scratch import of the 38,685-message
    archive found NULs before the production import."""
    return text.replace("\x00", "")


def clean_text(text: str) -> str:
    text = _WS.sub(" ", _strip_nul(text).replace("\r\n", "\n").replace("\r", "\n"))
    # A non-breaking space is a space. Left alone it survives every whitespace
    # rule here and reaches the reader as an invisible character that breaks
    # nothing and joins nothing.
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    return _BLANKS.sub("\n\n", text).strip()


def _clean_header(value: str | None) -> str | None:
    """Headers reach the database too, and carry the same hazard.

    Subject lines in particular: a mis-declared charset in an encoded-word
    decodes to bytes that can include NUL.
    """
    if value is None:
        return None
    cleaned = _strip_nul(value).strip()
    return cleaned or None


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n\n", html)
    # A block element ends a line. Without this, a table-laid-out mail - which
    # is most ATS mail - collapses into one run-on paragraph, and the sender's
    # own structure is lost rather than translated.
    html = re.sub(r"(?i)</(div|tr|td|li|h[1-6]|table)>", "\n", html)
    # Entities LAST, after tags are gone: unescaping first would turn a
    # literal "&lt;b&gt;" in the sender's text into a tag the stripper then
    # eats. Without it "&nbsp;" and "&amp;" reach the reader verbatim, which
    # is what a 67k-message corpus is currently full of.
    return clean_text(unescape(_TAG.sub(" ", html)))


def parse_sent_at(raw: str | None) -> datetime | None:
    """Aware UTC or nothing.

    A naive datetime here would be compared against `now()` in Postgres and be
    wrong by the container's offset - the trap that shifted every window in
    this product by four hours once already.
    """
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except TypeError, ValueError:
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
