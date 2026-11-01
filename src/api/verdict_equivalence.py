"""The models whose verdicts a reader accepts as `model`'s own
(app_config verdict_model_equivalence, core.verdict_equivalence)."""

from __future__ import annotations

from api import db
from core.verdict_equivalence import ModelEquivalence


def answering_models(kind: str, model: str) -> list[str]:
    return ModelEquivalence.model_validate(db.get_config("verdict_model_equivalence")).answering(
        kind, model
    )
