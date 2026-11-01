"""The one message shape both archive readers produce."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ImportedMessage:
    """One message, in the shape `email_messages` stores."""

    provider_message_id: str
    source: str
    provider_thread_id: str | None = None
    # Outlook's ThreadTopic: a normalised subject, NOT a conversation id. It
    # groups mail usefully within one employer and collides wildly across
    # employers, so it is kept and named for what it is rather than stored in
    # provider_thread_id, where every consumer would read it as identity.
    thread_topic: str | None = None
    from_email: str | None = None
    from_name: str | None = None
    to_emails: list[str] = field(default_factory=list)
    subject: str | None = None
    sent_at: datetime | None = None
    # Bodies are stored whole: always retain all data
    # (docs/agents/engineering-standards.md). A reader that feeds a model or a
    # display bounds what it reads, not what is kept.
    body_text: str | None = None
    # The markup it arrived as, kept so a reader can render mail as mail.
    # None means there was none, or that this source cannot supply it.
    body_html: str | None = None
    # The threading chain as the message carried it. provider_thread_id keeps
    # only the FIRST References entry, which is enough to group a reply with
    # its root and not enough to reconstruct a conversation that was forwarded,
    # split, or re-rooted. Keep it at parse time so reconstructing a thread
    # does not require rereading the 4.24 GB archive.
    headers: dict[str, str] = field(default_factory=dict)
