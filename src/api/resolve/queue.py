from __future__ import annotations

import datetime
from collections import Counter
from typing import Any

from api import db
from api.mail import pipeline as mail_pipeline
from api.resolve.choice_policy import by_company
from api.resolve.contracts import (
    ACTION_ITEM,
    ITEM_KINDS,
    STATUS_PROPOSAL,
    UNCONFIRMED_MATCH,
    UNMATCHED_MESSAGE,
)
from api.resolve.queue_items import action_items, match_items, message_items, proposal_items
from api.resolve.ranking import RANK_LABELS

_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)


def queue_for(
    owner_id: int, limit: int, offset: int, kinds: list[str] | None = None
) -> dict[str, Any]:
    wanted = set(kinds or ITEM_KINDS)
    apps = db.query(
        "SELECT id, company_name, title, applied_at FROM applications "
        "WHERE user_id = %s AND dismissed_at IS NULL",
        (owner_id,),
    )
    events = mail_pipeline.events_by_application(owner_id)

    items: list[dict[str, Any]] = []
    if UNMATCHED_MESSAGE in wanted:
        items += message_items(owner_id, by_company(apps), events)
    if UNCONFIRMED_MATCH in wanted:
        items += match_items(owner_id, events)
    if STATUS_PROPOSAL in wanted:
        items += proposal_items(owner_id, events)
    if ACTION_ITEM in wanted:
        items += action_items(owner_id, events)

    # RANKED BEFORE PAGED. Sorting inside a page would reorder fifty rows and
    # call it a ranking of three and a half thousand - the page would look
    # sensible and the ordering would be a lie.
    items.sort(key=lambda i: (i.get("message") or {}).get("sent_at") or _EPOCH, reverse=True)
    items.sort(key=lambda i: i["rank"], reverse=True)
    by_rank = Counter(i["rank"] for i in items)
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        # What is below the fold, so a page can say "40 need you, 3,623 do not"
        # rather than implying the first fifty are all there is.
        "by_rank": {RANK_LABELS[k]: v for k, v in sorted(by_rank.items(), reverse=True)},
        # Counted over everything asked for, never over the page.
        "by_kind": dict(Counter(i["kind"] for i in items)),
    }
