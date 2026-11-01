"""Stream Gmail mbox and Outlook .olm archives into one message shape.

The measured Takeout mbox was 4.24 GB and each .olm was about 4.4 GB. The
readers yield messages without indexing or loading an entire archive.

api/mail/gmail.py also calls clean_text, html_to_text, and parse_sent_at.
Live and imported mail therefore use the same normalization rules."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from core.mail.importer.mbox import read_mbox
from core.mail.importer.message import ImportedMessage
from core.mail.importer.olm import read_olm
from core.mail.importer.text import clean_text, html_to_text, parse_sent_at

__all__ = [
    "ImportedMessage",
    "clean_text",
    "html_to_text",
    "parse_sent_at",
    "read_archive",
    "read_mbox",
    "read_olm",
]


def read_archive(path: Path | str) -> Iterator[ImportedMessage]:
    """Dispatch on extension so callers never branch on format themselves."""
    name = str(path).lower()
    if name.endswith(".olm"):
        return read_olm(path)
    if name.endswith((".mbox", ".mbx", ".zip")):
        return read_mbox(path)
    raise ValueError(f"unsupported archive: {path}")
