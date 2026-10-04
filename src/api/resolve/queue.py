from __future__ import annotations

import datetime
from collections import Counter
from typing import Any

from api import db
from api.resolve.choice_policy import by_company
from api.resolve.contracts import (
    ACTION_ITEM,
    ITEM_KINDS,
    STATUS_PROPOSAL,
    UNCONFIRMED_MATCH,
    UNMATCHED_MESSAGE,
)
from api.resolve.queue_items import (
    Ranked,
    action_items,
    build,
    current_rows,
    events_by_application,
    match_items,
    message_items,
    proposal_items,
)
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
    apps_by_company = by_company(apps)
    rows = current_rows(owner_id)
    events = events_by_application(rows)

    ranked: list[Ranked] = []
    if UNMATCHED_MESSAGE in wanted:
        ranked += message_items(rows, apps_by_company, events)
    if UNCONFIRMED_MATCH in wanted:
        ranked += match_items(rows, events)
    if STATUS_PROPOSAL in wanted:
        ranked += proposal_items(rows)
    if ACTION_ITEM in wanted:
        ranked += action_items(owner_id, events)

    # RANKED BEFORE PAGED. Sorting inside a page would reorder fifty rows and
    # call it a ranking of three and a half thousand - the page would look
    # sensible and the ordering would be a lie. Only the page is BUILT: every
    # row is ranked and counted, and the columns a row shows are read for the
    # rows that are shown.
    ranked.sort(key=lambda r: r.sent_at or _EPOCH, reverse=True)
    ranked.sort(key=lambda r: r.rank, reverse=True)
    by_rank = Counter(r.rank for r in ranked)
    return {
        "items": build(owner_id, ranked[offset : offset + limit], apps_by_company, events),
        "total": len(ranked),
        # What is below the fold, so a page can say "40 need you, 3,623 do not"
        # rather than implying the first fifty are all there is.
        "by_rank": {RANK_LABELS[k]: v for k, v in sorted(by_rank.items(), reverse=True)},
        # Counted over everything asked for, never over the page.
        "by_kind": dict(Counter(r.kind for r in ranked)),
    }
