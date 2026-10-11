"""Run each person's mail matching stages in their required order.

The matcher first revisits current messages, then derives applications only
from evidence still unmatched after the sweep.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Any

from api import db
from api.mail import match as mail_match
from tasks.mail_match_derived import (
    APPLIED_KINDS,
    CAP_FLOOR,
    DERIVED,
    derived_cap,
    recompute_derived_floors,
    seed_from_mail,
)
from tasks.mail_match_sweep import PROGRESS_EVERY, detach_unattachable, match_pending
from tasks.runtime import set_progress

logger = logging.getLogger(__name__)

__all__ = [
    "APPLIED_KINDS",
    "CAP_FLOOR",
    "DERIVED",
    "PROGRESS_EVERY",
    "derived_cap",
    "detach_unattachable",
    "handle_match_mail",
    "match_pending",
    "recompute_derived_floors",
    "seed_from_mail",
]


def _step_progress(
    task_id: int, index: int, users: int, user_id: int, what: str, done: int, total: int
) -> None:
    set_progress(task_id, index, users, f"matching user {user_id}: {what} {done}/{total}")


async def handle_match_mail(task_id: int, payload: dict[str, Any]) -> None:
    """Match every user's mail, or one user's when asked.

    Scheduled runs carry no user_id: matching is per-user by construction (the
    candidate set is that user's applications) but the schedule is fleet-wide,
    and a task that silently did only user 1 would look identical to one that
    did everybody.
    """
    requested = payload.get("user_id")
    if requested:
        user_ids = [int(requested)]
    else:
        # Users with mail OR with applications: a person whose applications
        # come only from the board still has their action items resynced.
        user_ids = [
            r["user_id"]
            for r in db.query(
                """
                SELECT user_id FROM email_messages
                UNION
                SELECT user_id FROM applications
                ORDER BY user_id
                """
            )
        ]
    if not user_ids:
        set_progress(task_id, 0, 0, "no mail to match")
        return

    limit = payload.get("limit")
    # `swept` is how many messages were DECIDED, not how many were matched.
    # It was called "matched" and reported as "messages matched", which is the
    # sweep size wearing a success metric's name: a run that decided 5,004
    # messages and attached none of them read as "5004 messages matched", and a
    # run whose predicate selected nothing read as "0 messages matched" - which
    # was taken as evidence that a working change had recovered nothing and
    # came within one decision of reverting 1,506 corrections.
    totals = {
        "derived": 0,
        "swept": 0,
        "attached": 0,
        "detached": 0,
        "floors_lowered": 0,
    }
    skipped = 0
    for index, user_id in enumerate(user_ids):
        set_progress(task_id, index, len(user_ids), f"matching user {user_id}")
        step = partial(_step_progress, task_id, index, len(user_ids), user_id)
        # A partial run decides against everything, as it always did.
        since = None if limit else mail_match.last_sweep_start(user_id)
        if not mail_match.changed_since(since, user_id):
            skipped += 1
            continue
        totals["detached"] += detach_unattachable(user_id)
        # Before matching, so a floor lowered now releases its messages in the
        # same sweep rather than the next one.
        totals["floors_lowered"] += recompute_derived_floors(user_id)
        counts = match_pending(
            user_id,
            limit=int(limit) if limit else None,
            since=since,
            progress=partial(step, "messages"),
            progress_every=PROGRESS_EVERY,
        )
        totals["swept"] += sum(counts.values())
        # Messages that came out of this sweep holding an application. The
        # verdicts that attach nothing - `unmatched`, and the deliberate
        # refusals - are real outcomes but they are not matches, and counting
        # them as such is what made the two indistinguishable.
        totals["attached"] += sum(
            n
            for method, n in counts.items()
            if method not in (mail_match.UNMATCHED, mail_match.NOT_AN_APPLICATION)
        )
        created, _ = seed_from_mail(user_id)
        totals["derived"] += created

    summary = (
        f"{len(user_ids)} user(s): {totals['derived']} derived "
        f"applications, {totals['swept']} messages swept "
        f"({totals['attached']} now attached), "
        f"{totals['detached']} detached, "
        f"{totals['floors_lowered']} floors lowered"
        + (f", {skipped} unchanged since the last sweep" if skipped else "")
    )
    logger.info(f"Task {task_id}: {summary}")
    set_progress(task_id, len(user_ids), len(user_ids), summary)
