"""Interactive prompt improvement for a person's filter."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api import ai, budget
from api.ai import access as ai_access
from api.auth import AuthedUser, require_user
from api.models import ImprovePromptRequest
from api.problem import AI_REFUSALS, refuse

router = APIRouter()

IMPROVE_MODEL = os.environ.get("JOBTRACKER_IMPROVE_MODEL", "gpt-6-luna")


class ImprovedPrompt(BaseModel):
    """What the model wrote, and what it says it changed. The same shape it
    is asked for and the shape this route returns, because there is nothing
    in between."""

    improved: str
    rationale: str


@router.post("/ai/improve-prompt", responses=AI_REFUSALS)
async def improve_prompt(
    body: ImprovePromptRequest, user: AuthedUser = Depends(require_user)
) -> ImprovedPrompt:
    cfg = ai_access.require_config(user)

    if cfg.key_source == "owner":
        cfg.model = IMPROVE_MODEL if IMPROVE_MODEL in ai.OWNER_KEY_MODELS else cfg.model
    with budget.record_parse_failures(user.id, cfg.key_source, "improve_prompt", cfg.model):
        parsed, usage = await ai.parse(
            cfg,
            (
                "You rewrite rough job-filter prompts into the structure this system's "
                "best-performing filters use. A later AI reads one job posting plus the "
                "rewritten prompt and decides keep-or-filter, so the prompt must make that "
                "decision fast and unambiguous - decisive prompts also cost fewer reasoning "
                "tokens.\n\n"
                "Rewrite into this proven shape:\n"
                "1. One opening line stating who/what the filter is for, if inferable.\n"
                "2. A 'KEEP' section: concrete inclusions - role titles, domains, skills, "
                "company types. Name examples rather than describing vibes.\n"
                "3. A 'FILTER OUT' section: concrete exclusions, phrased either 'only when "
                "clearly ...' (lenient) or 'if ANY of these apply (each is sufficient on its "
                "own)' (strict) - pick whichever matches the user's evident intent.\n"
                "4. Numbers over adjectives: turn 'well paid' into a threshold with an explicit "
                "reading rule (e.g. 'judge on the TOP of a stated range'); turn 'junior' into "
                "an experience/degree rule with required-vs-preferred distinguished.\n"
                "5. End with one explicit ambiguity rule: 'When uncertain, KEEP.' or 'If you "
                "cannot confirm the criteria, FILTER OUT.' - matching the user's intent "
                "(default to KEEP when unclear which they want).\n\n"
                "Keep the user's intent exactly; do not invent criteria they did not imply. "
                "Do not add instructions about output format or reasons - the system appends "
                "those. rationale: <=50 words on what you changed and why."
            ),
            body.prompt,
            ImprovedPrompt,
            timeout=60.0,
        )
    budget.record_tokens(
        user.id,
        cfg.key_source,
        "improve_prompt",
        cfg.model,
        usage,
    )
    if not parsed:
        raise refuse(502, "AI_ERROR", "no response from model")
    return parsed
