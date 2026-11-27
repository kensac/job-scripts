"""Fit a person's taste model (core.taste).

`train_taste` fits one person's model. It is asked for by the For you page
when the person has none or theirs is a day old, and once a day for everyone
who has one, so a model follows what its person applies to.
"""

from __future__ import annotations

import asyncio
from typing import Any

from api import db
from api.queue import enqueue
from core import taste
from core.derivations.local_embeddings import LOCAL_EMBEDDING_MODEL
from tasks.runtime import set_progress


async def handle_train_taste(task_id: int, payload: dict[str, Any]) -> None:
    user_id = int(payload["user_id"])
    window = int(db.get_config("recommendation_window_days"))
    set_progress(task_id, 0, 1, f"fitting user {user_id}'s taste model")
    stored = await asyncio.to_thread(taste.train, user_id, window, LOCAL_EMBEDDING_MODEL)
    evaluation = stored["evaluation"]
    set_progress(
        task_id,
        1,
        1,
        f"taste model {stored['id']}: {evaluation['held_out_in_top_share']:.0%} of held-out "
        f"applications in the top {evaluation['top_share']:.0%}",
        extra=stored,
    )
    # The ranking the For you page reads, recomputed with the new model.
    enqueue(
        "rank_for_you",
        {"user_id": user_id, "model_id": stored["id"]},
        dedupe_key=f"rank-for-you:{user_id}:model-{stored['id']}",
    )
