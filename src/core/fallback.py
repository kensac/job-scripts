"""Moving a task to the next model when a provider says the account cannot pay.

Only provider-reported exhaustion moves work: out of credit, a billing hard
limit, a quota used up. Each vendor's datasheet declares the fragments that
mean it (`Provider.exhaustion_markers`). A rate limit, a 5xx or a timeout is
not exhaustion and keeps its own retry, because the next model would be paid
for work the first would have done a minute later.

The chain is an admin's ordered list per purpose (app_config
model_fallback_chains), empty by default, so nothing moves until one is set.
Every entry is still judged by routing for the task's declared needs: a model
that cannot take the batch endpoint, the structured mode or the effort is
skipped, never sent.
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Sequence

from pydantic import RootModel, model_validator

from core import providers
from core.providers.spec import StructuredOutput
from core.routing import Choice, NoEligibleModel, TaskShape, resolve

# The purposes a chain may name: every configurable task, and the personal
# filter, whose model is each person's own setting rather than a TaskShape.
# Managed boards are absent on purpose: a board's model is part of its
# published contract, and its membership reads only that model's verdicts, so
# answers from another model would be paid for and never shown.
FILTER_PURPOSE = "filter"

# The output cap a filter batch sends; a boolean verdict has never needed more
# than 1,050.
FILTER_MAX_OUTPUT_TOKENS = 6000
# Mean billed input of a filter request, 269,567 calls over the 30 days to
# 2026-11-01. It only ranks and prices a fallback; the bill is written from
# real usage.
FILTER_EST_PROMPT_TOKENS = 2514


def filter_shape(model: str, effort: str | None) -> TaskShape:
    """What a personal filter batch needs, for judging a fallback model.

    The person's model is their own setting, not a declared shape, so this is
    built from it. The effort is the person's: a fallback that does not accept
    it is not eligible, rather than silently asked something else.
    """
    return TaskShape(
        purpose=FILTER_PURPOSE,
        structured=StructuredOutput.JSON_SCHEMA,
        batched=True,
        max_output_tokens=FILTER_MAX_OUTPUT_TOKENS,
        est_prompt_tokens=FILTER_EST_PROMPT_TOKENS,
        candidates=(model,),
        effort=effort,
    )


def exhaustion_reason(provider: str | None, error: BaseException | str) -> str | None:
    """The provider's own words when `error` says the account cannot pay, else None.

    Reads the exception's text, and its `code` and `body` when the SDK keeps
    them apart from the message: OpenAI's text holds only the message, so
    insufficient_quota is found in the code.
    """
    known = providers.PROVIDERS.get(provider or "")
    if known is None or not known.exhaustion_markers:
        return None
    message = str(error)
    haystack = " ".join(
        str(part)
        for part in (message, getattr(error, "code", None), getattr(error, "body", None))
        if part
    ).lower()
    if any(marker in haystack for marker in known.exhaustion_markers):
        return message or haystack
    return None


class ProviderExhausted(RuntimeError):
    """A provider refused a submission because the account cannot pay.

    Carries what a caller needs to move the rest elsewhere: the waves already
    accepted (paid, and to be kept) and the requests never sent.
    """

    def __init__(self, provider: str, reason: str, accepted: list[str], unsubmitted: list):
        prefix = "no batches were accepted by the provider" if not accepted else "provider refused"
        super().__init__(f"{prefix}: {reason}")
        self.provider = provider
        self.reason = reason
        self.accepted = accepted
        self.unsubmitted = unsubmitted


def chain_purposes() -> list[str]:
    """Every purpose a chain may name, in display order."""
    from core.shapes import SHAPES

    # job_profile's model is fixed by the profiles it stores
    # (core.derivations.job_profile), so it never falls back.
    return [*sorted(SHAPES.keys() - {"job_profile"}), FILTER_PURPOSE]


class FallbackChains(RootModel[dict[str, list[str]]]):
    """Purpose -> the models to try, in order, when the running one's provider
    is out of credit."""

    root: dict[str, list[str]] = {}

    @model_validator(mode="after")
    def _known(self) -> FallbackChains:
        purposes = chain_purposes()
        for purpose, models in self.root.items():
            if purpose not in purposes:
                raise ValueError(f"unknown purpose {purpose!r}; one of {sorted(purposes)}")
            if len(set(models)) != len(models):
                raise ValueError(f"{purpose}: a chain repeats a model")
            for name in models:
                if name not in providers.MODELS:
                    raise ValueError(f"{purpose}: unknown model {name!r}")
        return self


def next_choice(
    shape: TaskShape,
    chain: list[str],
    blocked: set[str],
    tried: set[str],
    prefer: Sequence[str] = (),
    at: datetime.datetime | None = None,
) -> Choice | None:
    """The first chain entry that can run this task now, or None.

    Entries whose verdicts count as the primary's (`prefer`, the verdict
    equivalence group) go first, each side in the admin's order. An entry is
    skipped when its provider is in `blocked`, when it was already tried, or
    when routing rejects it for this shape.
    """
    for name in sorted(chain, key=lambda m: m not in prefer):
        if name in tried or providers.provider_of(name) in blocked:
            continue
        try:
            choice = resolve(dataclasses.replace(shape, candidates=(name,)), at)
        except NoEligibleModel:
            continue
        return dataclasses.replace(choice, reason="fallback chain")
    return None
