"""Build verification requests and identify the exact question asked."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from api import managed_board_runs
from api.task_config import configured_model, configured_shape
from core.answers import VERIFICATION_REQUEST, joint_verification
from core.batch import BatchSpec, structured_response_spec
from core.filters import build_custom_input
from core.routing import resolve
from core.shapes import VERIFY_TASK

# Both sweeps submit at the shape's effort; reverify spells the fallback.
_EFFORT = VERIFY_TASK.effort or "low"


def _verification_spec(url: str, content: str, context: dict | None = None) -> BatchSpec:
    return structured_response_spec(
        url,
        VERIFICATION_REQUEST.instructions,
        VERIFICATION_REQUEST.build_input(content),
        VERIFICATION_REQUEST.response_model,
        context=context,
    )


def _submission_model() -> tuple[str, str]:
    """The model and effort run_batched will submit the new-posting sweep with."""
    shape = configured_shape(VERIFY_TASK)
    chosen = resolve(shape, override=configured_model(VERIFY_TASK.purpose))
    return chosen.model, str(chosen.params.get("reasoning_effort") or shape.resolved_effort() or "")


def _joint_spec(
    row: dict[str, Any],
    questions: list[managed_board_runs.BoardQuestion],
    model: str,
    effort: str,
) -> BatchSpec:
    """Verification plus the boards' questions, reading the posting once."""
    instructions, response_model = joint_verification(
        {question.board_id: question.criteria for question in questions}
    )
    content = VERIFICATION_REQUEST.build_input(row["input_content"])
    return structured_response_spec(
        row["url"],
        instructions,
        build_custom_input(row["company"], row["title"], content),
        response_model,
        context={
            **{
                key: row[key]
                for key in ("company", "title", "needs_closed", "needs_clearance", "page_fetch_id")
            },
            "model": model,
            "effort": effort,
            "verify_question": question_sha256(
                model, _verification_spec(row["url"], row["input_content"])
            ),
            "boards": [
                {"board_id": q.board_id, "prompt_hash": q.prompt_hash, "model": q.model}
                for q in questions
            ],
        },
    )


def question_sha256(model: str | None, request: BatchSpec | None) -> str | None:
    """Identity of the question a verification answers, or None if unknown.

    Everything that could change the answer is in it: the model, the effort,
    the schema, the instructions and the exact page text. Hashed from the
    request as submitted (a collected result carries its frozen snapshot), so
    a prompt edit or a model change is a different question and is paid for.
    """
    if not model or request is None:
        return None
    digest = hashlib.sha256()
    for part in (
        model,
        _EFFORT,
        request.schema_name,
        json.dumps(request.schema, sort_keys=True),
        request.instructions,
        request.input,
    ):
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()
