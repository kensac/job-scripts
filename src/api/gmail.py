"""Reading a user's Gmail, read-only and nothing else.

Scope is gmail.readonly and the code here issues list and get only. There is
deliberately no send, no modify, and no delete path - not as policy but as
absence, so no future edit can quietly acquire one.

Incremental sync uses Gmail's historyId rather than a date window. A window
re-fetches everything near its edge on every run and still misses anything
that arrives out of order; a historyId is the provider's own "what changed
since" cursor and cannot skip.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator
from typing import Any

import requests

from api import oauth
from core.mail_import import MAX_BODY_CHARS, ImportedMessage, _clean, _html_to_text, _sent_at

logger = logging.getLogger("jobtracker_api")

_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Every Gmail call is bounded. An ingest sweep makes thousands of these, so an
# unbounded one turns a slow provider into a stalled worker holding a task.
HTTP_TIMEOUT = 30.0

# Gmail caps list responses at 500 and charges the same quota per page either
# way, so asking for fewer would only add round trips.
PAGE_SIZE = 500


class GmailError(RuntimeError):
    """The provider failed in a way the caller should surface, not swallow."""


def _get(path: str, token: str, **params: Any) -> dict[str, Any]:
    try:
        resp = requests.get(
            f"{_API}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or None,
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise GmailError(f"gmail unreachable: {type(exc).__name__}") from exc
    if resp.status_code == 401:
        # The token was refused mid-sweep. This is the seven-day death, and it
        # must reach the caller as the reconnect signal rather than as a
        # generic provider error that a retry would paper over.
        raise oauth.NeedsReconnect("gmail rejected the access token")
    if resp.status_code >= 400:
        raise GmailError(f"gmail {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _header(payload: dict[str, Any], name: str) -> str | None:
    for header in payload.get("headers") or ():
        if header.get("name", "").lower() == name.lower():
            return header.get("value")
    return None


def _decode(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _walk_parts(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield payload
    for part in payload.get("parts") or ():
        yield from _walk_parts(part)


def _body(payload: dict[str, Any]) -> str | None:
    """text/plain if present, else HTML converted.

    ATS mail is frequently HTML-only, so preferring plain text but accepting
    HTML is the difference between reading most rejections and missing them.
    """
    plain: list[str] = []
    html: list[str] = []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        data = _decode((part.get("body") or {}).get("data"))
        if not data:
            continue
        if mime == "text/plain":
            plain.append(data)
        elif mime == "text/html":
            html.append(data)
    text = "\n".join(plain) if plain else _html_to_text("\n".join(html))
    return _clean(text)[:MAX_BODY_CHARS] or None


def to_imported(message: dict[str, Any]) -> ImportedMessage:
    """Gmail's shape into the one every archive reader also produces.

    Message-ID rather than Gmail's own id, so a message already imported from
    Takeout or an .olm export dedupes against this one instead of arriving
    twice under two identities.
    """
    payload = message.get("payload") or {}
    rfc_id = (_header(payload, "Message-ID") or "").strip()
    to_line = ", ".join(v for v in (_header(payload, "To"), _header(payload, "Cc")) if v)
    from email.utils import getaddresses

    sender = getaddresses([_header(payload, "From") or ""])
    from_name, from_email = sender[0] if sender else ("", "")
    return ImportedMessage(
        provider_message_id=rfc_id or f"gmail-{message.get('id')}",
        source="gmail",
        provider_thread_id=message.get("threadId"),
        from_email=from_email or None,
        from_name=from_name or None,
        to_emails=[addr for _, addr in getaddresses([to_line]) if addr],
        subject=(_header(payload, "Subject") or "").strip() or None,
        sent_at=_sent_at(_header(payload, "Date")),
        body_text=_body(payload),
    )


def list_message_ids(user_id: int, *, after: str | None = None) -> Iterator[str]:
    """Every message id in the mailbox, oldest page first.

    No query filter: the prefilter is a signal rather than a gate, so the
    classifier sees the whole mailbox and the decision about what matters is
    made once, downstream, where it can be revised.
    """
    token = oauth.get_access_token(user_id)
    page: str | None = None
    while True:
        params: dict[str, Any] = {"maxResults": PAGE_SIZE, "includeSpamTrash": True}
        if page:
            params["pageToken"] = page
        if after:
            params["q"] = f"after:{after}"
        data = _get("/messages", token, **params)
        for message in data.get("messages") or ():
            if message.get("id"):
                yield message["id"]
        page = data.get("nextPageToken")
        if not page:
            return


def fetch_message(user_id: int, message_id: str) -> ImportedMessage:
    token = oauth.get_access_token(user_id)
    return to_imported(_get(f"/messages/{message_id}", token, format="full"))


def profile(user_id: int) -> dict[str, Any]:
    """Mailbox identity and the current historyId, which is the sync cursor."""
    return _get("/profile", oauth.get_access_token(user_id))
