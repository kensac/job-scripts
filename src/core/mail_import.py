"""Reading mail archives into one normalised shape.

Two formats arrive: Gmail Takeout gives an mbox, Outlook for Mac gives .olm (a
ZIP of per-message XML). They are different enough to tempt two code paths and
similar enough that two paths would drift - this codebase produced five
separate cases of duplicated logic drifting apart in a single evening, so the
formats converge here and nothing downstream learns which one a message came
from.

Both readers STREAM. The Takeout mbox is 4.24 GB and each .olm is ~4.4 GB;
anything that builds an index or holds the archive in memory is unusable on
these files, which is why python's own `mailbox.mbox` is not used - it indexes
the whole file before yielding anything.
"""

from __future__ import annotations

import email
import logging
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

# Bodies are stored to be read by a model and by a human in the debug view.
# Past this length a job email is quoted history, signatures and legal
# boilerplate, none of which changes a classification - and the tail is what
# makes 38,685 messages expensive.
logger = logging.getLogger("jobtracker_api")

MAX_BODY_CHARS = 20_000

_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class ImportedMessage:
    """One message, in the shape `email_messages` stores."""

    provider_message_id: str
    source: str
    provider_thread_id: str | None = None
    from_email: str | None = None
    from_name: str | None = None
    to_emails: list[str] = field(default_factory=list)
    subject: str | None = None
    sent_at: datetime | None = None
    body_text: str | None = None


def _clean(text: str) -> str:
    text = _WS.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n"))
    return _BLANKS.sub("\n\n", text).strip()


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</p>", "\n\n", html)
    return _clean(_TAG.sub(" ", html))


def _body(msg: Message) -> str | None:
    """Prefer text/plain; fall back to stripped HTML.

    Marketing and ATS mail is frequently HTML-only, so a plain-text-only reader
    would silently drop a large share of exactly the messages that matter.
    """
    try:
        part = msg.get_body(preferencelist=("plain", "html"))  # type: ignore[attr-defined]
    except Exception:
        part = None
    if part is not None:
        try:
            content = part.get_content()
        except Exception:
            content = None
        if isinstance(content, str):
            text = content if part.get_content_type() == "text/plain" else _html_to_text(content)
            return _clean(text)[:MAX_BODY_CHARS] or None
    try:
        payload = msg.get_payload(decode=True)
    except Exception:
        payload = None
    if isinstance(payload, bytes):
        return _clean(payload.decode("utf-8", errors="replace"))[:MAX_BODY_CHARS] or None
    return None


def _sent_at(raw: str | None) -> datetime | None:
    """Aware UTC or nothing.

    A naive datetime here would be compared against `now()` in Postgres and be
    wrong by the container's offset - the trap that shifted every window in
    this product by four hours once already.
    """
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _from_message(msg: Message, *, source: str, fallback_id: str) -> ImportedMessage:
    sender = getaddresses([str(msg.get("From", "") or "")])
    from_name, from_email = sender[0] if sender else ("", "")
    recipients = [
        addr
        for _, addr in getaddresses([str(msg.get("To", "") or ""), str(msg.get("Cc", "") or "")])
        if addr
    ]
    return ImportedMessage(
        # Message-ID is the only cross-archive stable identity available. The
        # four .olm exports overlap heavily with each other and with Takeout,
        # so without it the same message imports many times.
        provider_message_id=(str(msg.get("Message-ID", "") or "").strip() or fallback_id),
        source=source,
        provider_thread_id=(str(msg.get("References", "") or "").split() or [None])[0],
        from_email=(from_email or None),
        from_name=(from_name or None),
        to_emails=recipients,
        subject=(str(msg.get("Subject", "") or "").strip() or None),
        sent_at=_sent_at(str(msg.get("Date", "") or "") or None),
        body_text=_body(msg),
    )


def read_mbox(path: Path | str, *, source: str = "takeout") -> Iterator[ImportedMessage]:
    """Stream an mbox without indexing it.

    Messages are separated by a line beginning "From " at column zero. That
    marker can also legitimately appear inside a body, which is why a real
    mbox escapes it as ">From " - so an unescaped one is a boundary.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
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


def read_olm(path: Path | str, *, source: str = "olm") -> Iterator[ImportedMessage]:
    """Stream an Outlook for Mac archive.

    .olm is a ZIP whose message XML lives under Accounts/.../Message/. Each
    file holds one or more <email> elements. Entries are read one at a time so
    a 4.4 GB archive never lands in memory.
    """
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".xml"):
                continue
            try:
                with zf.open(info) as fh:
                    raw = fh.read()
            except Exception:
                logger.warning("olm: unreadable entry %s, skipping", info.filename)
                continue
            yield from _olm_entries(raw, source=source, origin=info.filename)


def _olm_text(node: ElementTree.Element, *names: str) -> str | None:
    for name in names:
        found = node.find(name)
        if found is not None and (found.text or "").strip():
            return (found.text or "").strip()
    return None


_DOCTYPE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)


def _olm_parse(raw: bytes) -> ElementTree.Element | None:
    """Parse one .olm XML entry, refusing anything carrying a DOCTYPE.

    The archive is generated by Outlook from the owner's own mailbox, so it is
    not untrusted input in the usual sense - but message CONTENT inside it came
    from arbitrary senders, and ElementTree does expand internal entities
    (verified: `<!ENTITY a "boom">` resolves), so a billion-laughs bomb would
    hang a 38,685-message import with no error and nothing pointing at the
    cause.

    Entity expansion requires a DOCTYPE internal subset, and Outlook does not
    emit a DOCTYPE, so refusing them closes the whole class at the cost of
    nothing legitimate. This is deliberately a content check rather than a
    parser hook: ElementTree's XMLParser exposes no handler to install one on,
    and pulling in defusedxml for a single call site is not worth a dependency.
    """
    if _DOCTYPE.search(raw[:4096]):
        return None
    try:
        return ElementTree.fromstring(raw)  # noqa: S314 - DOCTYPE refused above
    except ElementTree.ParseError:
        return None


def _olm_entries(raw: bytes, *, source: str, origin: str) -> Iterator[ImportedMessage]:
    root = _olm_parse(raw)
    if root is None:
        logger.warning("olm: unparsable xml in %s, skipping", origin)
        return
    for idx, node in enumerate(root.iter("email")):
        body = _olm_text(node, "OPFMessageCopyBody")
        html = _olm_text(node, "OPFMessageCopyHTMLBody")
        text = body or (_html_to_text(html) if html else None)
        addresses = [
            (e.get("OPFContactEmailAddressAddress") or "")
            for e in node.iter("emailAddress")
            if e.get("OPFContactEmailAddressAddress")
        ]
        sender = _olm_sender(node)
        yield ImportedMessage(
            provider_message_id=(
                _olm_text(node, "OPFMessageCopyMessageID") or f"{source}-{origin}-{idx}"
            ),
            source=source,
            provider_thread_id=_olm_text(node, "OPFMessageCopyThreadTopic"),
            from_email=sender,
            from_name=_olm_text(node, "OPFMessageCopyFromAddresses"),
            to_emails=[a for a in addresses if a != sender],
            subject=_olm_text(node, "OPFMessageCopySubject"),
            sent_at=_olm_sent_at(node),
            body_text=(_clean(text)[:MAX_BODY_CHARS] if text else None),
        )


def _olm_sender(node: ElementTree.Element) -> str | None:
    holder = node.find("OPFMessageCopyFromAddresses")
    if holder is None:
        return None
    for addr in holder.iter("emailAddress"):
        value = addr.get("OPFContactEmailAddressAddress")
        if value:
            return value
    return None


def _olm_sent_at(node: ElementTree.Element) -> datetime | None:
    raw = _olm_text(node, "OPFMessageCopySentTime", "OPFMessageCopyReceivedTime")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _sent_at(raw)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def read_archive(path: Path | str) -> Iterator[ImportedMessage]:
    """Dispatch on extension so callers never branch on format themselves."""
    name = str(path).lower()
    if name.endswith(".olm"):
        return read_olm(path)
    if name.endswith((".mbox", ".mbx")):
        return read_mbox(path)
    raise ValueError(f"unsupported archive: {path}")
