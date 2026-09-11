from __future__ import annotations

from typing import Any

from api.mail import pipeline as mail_pipeline

# What a row is worth deciding, highest first. Ordering rather than scoring,
# and every step of it derived rather than weighted:
#
#   3  answering it changes what the product says
#   2  it can be answered, but nothing changes on its own
#   1  only a refusal is available
#
# The top rank is the whole point. A rejection landing on an application still
# showing "applied" changes what the board says; an acknowledgement landing on
# the same application changes nothing, because stage is derived from the
# strongest event and an acknowledgement is never the strongest. Sorting by
# recency alone put those two side by side and let 2,884 year-old rows bury
# the forty that arrived this month.
#
# The same question is asked of every kind, which is what lets one number order
# four of them: would answering this change something a person would see.
#
# NOTHING IS HIDDEN. Every row is still returned and `total` still counts them
# all, because nothing in this population is unresolvable - a person can refuse
# any of it, so "low priority" is the honest claim and "cannot be settled" is
# not. That distinction belongs to action items, and they carry it as
# `settles_on` rather than as a rank, because measured over the corpus a rank
# could not carry it: the only kind with an empty settling set is
# `reply_to_recruiter`, and all 73 of those are already closed. Ranking on it
# would have looked principled and sorted nothing.
RANK_MOVES_STAGE = 3
RANK_ATTACHABLE = 2
RANK_REFUSAL_ONLY = 1

# The bucket labels `by_rank` is keyed by. Kind-neutral, because the queue holds
# four kinds and three of them are not about attaching anything - a bucket
# labelled "can be attached" would misdescribe every action item in it. The
# specific sentence lives on the row, in `rank_reason`.
RANK_LABELS = {
    RANK_MOVES_STAGE: "answering this changes what the product says",
    RANK_ATTACHABLE: "answerable, but nothing changes on its own",
    RANK_REFUSAL_ONLY: "only a refusal is available",
}

# Why one unmatched message sits where it does, which is a narrower claim than
# the bucket it lands in.
MESSAGE_RANK_REASONS = {
    RANK_MOVES_STAGE: "answering this moves an application",
    RANK_ATTACHABLE: "can be attached, but the stage would not move",
    RANK_REFUSAL_ONLY: "no application at this company yet",
}


def _stage_would_move(kind: str | None, own: list[mail_pipeline.ApplicationEvent]) -> bool:
    """Would adding an event of this kind change this application's stage?

    Asked of the same function the board reads, over the application's real
    events, rather than a second table saying which kinds count - so it cannot
    disagree with what the board will show once the person answers.

    `id` matters: stage_for breaks ties among terminal events by taking the
    newest, so a hypothetical event has to look newer than the real ones or a
    rejection already present would win over the one being considered and the
    answer would be "changes nothing" for the exact case that changes the most.
    """
    if not kind:
        return False
    before = mail_pipeline.stage_for(own)
    if before in mail_pipeline.TERMINAL:
        return False
    newest = max(e.id for e in own) if own else 0
    after = mail_pipeline.stage_for(
        [*own, mail_pipeline.ApplicationEvent.hypothetical(kind, newest + 1)]
    )
    return after != before


def rank(
    kind: str,
    candidates: list[dict[str, Any]],
    events: dict[int, list[mail_pipeline.ApplicationEvent]],
) -> int:
    if not candidates:
        return RANK_REFUSAL_ONLY
    for app in candidates:
        if _stage_would_move(kind, events.get(app["id"], [])):
            return RANK_MOVES_STAGE
    return RANK_ATTACHABLE
