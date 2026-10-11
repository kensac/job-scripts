"""Row types shared by resolve queue selection and page rendering."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, NamedTuple


class QueueEvent(NamedTuple):
    """An event as the queue reads it: what `stage_for` needs, and which
    message it came through so a match can be weighed without it."""

    id: int
    kind: str
    message_id: int


@dataclass(frozen=True, slots=True)
class Ranked:
    """A queue row before it is built: enough to order it and count it.

    The queue ranks before it pages, so every row has to be ranked, but only
    the page is shown. `row` is the ranking row, or for an action item the
    finished item, since those are few and complete in one statement.
    """

    kind: str
    rank: int
    sent_at: datetime.datetime | None
    row: dict[str, Any]
