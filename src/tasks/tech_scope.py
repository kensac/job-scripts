"""Keep the tech scope (core.tech_scope) current.

`train_tech_scope`, once a day, fits the scope model on the day's recent
postings. `score_tech_scope`, every cycle, scopes the recent embedded postings
that have no scope yet, whose embedding is newer than their scope, or whose
text-decided scope came from an older model, and writes only rows that change.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from api import db
from core import tech_scope
from core.derivations.local_embeddings import LOCAL_EMBEDDING_MODEL
from tasks.runtime import set_progress

logger = logging.getLogger(__name__)


async def handle_train_tech_scope(task_id: int, payload: dict[str, Any]) -> None:
    window = int(db.get_config("recommendation_window_days"))
    set_progress(task_id, 0, 1, "fitting the tech scope model")
    stored = await asyncio.to_thread(tech_scope.train, window, LOCAL_EMBEDDING_MODEL)
    set_progress(
        task_id,
        1,
        1,
        f"tech scope model {stored['id']}: held-out ROC AUC "
        f"{stored['evaluation']['held_out_roc_auc']}",
        extra=stored,
    )


async def handle_score_tech_scope(task_id: int, payload: dict[str, Any]) -> None:
    window = int(db.get_config("recommendation_window_days"))
    written = await asyncio.to_thread(tech_scope.score, window)
    set_progress(
        task_id, written, written, f"scoped {written} postings", extra={"written": written}
    )
    logger.info("score_tech_scope: %s rows written", written)
