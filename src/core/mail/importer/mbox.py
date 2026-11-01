"""Read Gmail Takeout mbox archives."""

from __future__ import annotations

import email
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from email import policy
from email.message import Message
from email.utils import getaddresses
from pathlib import Path

from core.mail.importer.message import ImportedMessage
from core.mail.importer.text import clean_header, clean_text, html_to_text, parse_sent_at

# Only the headers that describe threading. Storing every header would carry
# routing trace, spam scores and DKIM signatures for 67k messages to answer a
# question none of them are about.
_THREADING_HEADERS = ("Message-ID", "References", "In-Reply-To")


def _header(msg: Message, name: str) -> str:
    """A header as text, empty when the message does not carry it."""
    return str(msg.get(name, "") or "")


def _threading_headers(msg: Message) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in _THREADING_HEADERS:
        value = clean_header(_header(msg, name))
        if value:
            out[name.lower()] = value
    return out


def _part_content(msg: Message, kind: str) -> str | None:
    try:
        part = msg.get_body(preferencelist=(kind,))  # type: ignore[attr-defined]
    except Exception:
        return None
    if part is None:
        return None
    try:
        content = part.get_content()
    except Exception:
        return None
    return content if isinstance(content, str) else None


def _body(msg: Message) -> tuple[str | None, str | None]:
    """The readable text, and the markup it came from.

    Text prefers text/plain and falls back to stripped HTML - marketing
    and ATS mail is frequently HTML-only, so a plain-text-only reader would
    drop a large share of exactly the messages that matter.

    The HTML alternative is kept alongside, whichever part supplied the text:
    a multipart message usually carries both, and the plain part winning for
    the classifier is no reason to throw away the part a reader wants to see.
    """
    plain = _part_content(msg, "plain")
    html = _part_content(msg, "html")
    if plain is not None:
        text = plain
    elif html is not None:
        text = html_to_text(html)
    else:
        try:
            payload = msg.get_payload(decode=True)
        except Exception:
            payload = None
        if not isinstance(payload, bytes):
            return None, None
        text = payload.decode("utf-8", errors="replace")
    return clean_text(text) or None, html or None


def _from_message(msg: Message, *, source: str, fallback_id: str) -> ImportedMessage:
    sender = getaddresses([_header(msg, "From")])
    from_name, from_email = sender[0] if sender else ("", "")
    recipients = [
        addr for _, addr in getaddresses([_header(msg, "To"), _header(msg, "Cc")]) if addr
    ]
    text, html = _body(msg)
    return ImportedMessage(
        # Message-ID is the only cross-archive stable identity available. The
        # four .olm exports overlap heavily with each other and with Takeout,
        # so without it the same message imports many times.
        provider_message_id=clean_header(_header(msg, "Message-ID")) or fallback_id,
        source=source,
        provider_thread_id=clean_header(next(iter(_header(msg, "References").split()), None)),
        from_email=clean_header(from_email),
        from_name=clean_header(from_name),
        to_emails=[r for r in (clean_header(a) for a in recipients) if r],
        subject=clean_header(_header(msg, "Subject")),
        sent_at=parse_sent_at(_header(msg, "Date")),
        body_text=text,
        body_html=html,
        headers=_threading_headers(msg),
    )


@contextmanager
def _mbox_lines(path: Path | str) -> Iterator[Iterator[str]]:
    """Text lines from an mbox, or from the one inside a Takeout zip.

    Reading the zip member directly rather than extracting it first: the mbox
    is 4.24GB and the machine holding these archives was at 98% disk, so a
    temporary copy is a real risk for no benefit. Both paths stream, so neither
    holds the mailbox in memory.
    """
    if str(path).lower().endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".mbox")]
            if not members:
                raise ValueError(f"no .mbox inside {path}")
            if len(members) > 1:
                # Refused rather than guessed. Picking one silently is how a
                # partial archive gets imported as though it were the whole
                # mailbox.
                raise ValueError(f"{len(members)} .mbox files inside {path}: {sorted(members)}")
            with archive.open(members[0]) as raw:
                yield (line.decode("utf-8", errors="replace") for line in raw)
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        yield fh


def read_mbox(path: Path | str, *, source: str = "takeout") -> Iterator[ImportedMessage]:
    """Stream an mbox without indexing it.

    Messages are separated by a line beginning "From " at column zero. That
    marker can also legitimately appear inside a body, which is why a real
    mbox escapes it as ">From " - so an unescaped one is a boundary.
    """
    with _mbox_lines(path) as fh:
        buf: list[str] = []
        seq = 0
        for line in fh:
            if line.startswith("From ") and buf:
                seq += 1
                parsed = _parse_lines(buf, source=source, seq=seq)
                if parsed is not None:
                    yield parsed
                buf = []
            buf.append(line)
        if buf:
            seq += 1
            parsed = _parse_lines(buf, source=source, seq=seq)
            if parsed is not None:
                yield parsed


def _parse_lines(lines: list[str], *, source: str, seq: int) -> ImportedMessage | None:
    try:
        msg = email.message_from_string("".join(lines), policy=policy.default)
    except Exception:
        # One unparsable message must not abort a 38,685-message import.
        return None
    return _from_message(msg, source=source, fallback_id=f"{source}-seq-{seq}")
