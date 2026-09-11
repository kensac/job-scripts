"""The model a person has configured for a task, if any.

Below the handlers, beside the router that writes these rows. It was inside
the task runtime, which meant `api.budget` had to import a handler module to
price the fleet at the models the fleet is actually running.
"""

from __future__ import annotations

import logging

from api import db

logger = logging.getLogger(__name__)


def configured_model(purpose: str) -> str | None:
    """The model a person has configured for this task, if any.

    Read here rather than inside resolve() because core does not import api and
    an override lives in the database. resolve() takes it as an argument and
    stays a pure function of the declaration plus one value, which is what lets
    the configuration screen ask "what would this do" without a write.

    Latest row wins, and the table is append-only, so the history a monthly
    review needs is the table itself rather than something reconstructed.
    Returns None on any failure: a configuration lookup that cannot be read
    must fall back to the call site's own judgment rather than stopping a sweep.
    """
    try:
        row = db.query_one(
            "SELECT model FROM task_model_overrides WHERE purpose = %s ORDER BY id DESC LIMIT 1",
            (purpose,),
        )
    except Exception:
        logger.warning(f"could not read the configured model for {purpose}", exc_info=True)
        return None
    return (row or {}).get("model") or None
