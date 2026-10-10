"""Mail state that only a writer outside the owners could have produced."""

from __future__ import annotations

from typing import Any

from api import db
from api.mail import current


def _detect_mail() -> list[dict[str, Any]]:
    """A message whose current event or match pointer is not the newest row of
    its log. The writers move the pointer in the statement that appends, so
    after the backfill has finished once a lag means something appended
    another way. Quiet while a backfill is queued or running, and before the
    first one finishes, when lag is the expected state."""
    if not db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'backfill_mail_pointers' AND status = 'done' LIMIT 1"
    ) or db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'backfill_mail_pointers' "
        "AND status IN ('pending', 'running') LIMIT 1"
    ):
        return []
    stale = current.stale_pointers()
    if not stale:
        return []
    return [
        {
            "kind": "mail_pointer_stale",
            "subject": "email_messages",
            "severity": "warning",
            "message": (
                f"{stale} message(s) have a current event or match pointer that is not the "
                "newest row of its log. Something appended without mail.events.append or "
                "mail.match.record. The next backfill_mail_pointers run repairs the rows; "
                "the writer still needs finding."
            ),
            "detail": {"stale": stale},
        }
    ]
