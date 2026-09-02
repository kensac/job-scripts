"""Which model answers a given piece of work, and why.

Every call site used to name a model as a bare string. That made four things
invisible: whether the model can do what the task needs, whether a key for it
exists, what it costs at the moment it runs, and on what grounds it was chosen
over the alternatives. This resolves a task's declared needs against the
declared capabilities in core/providers/ and answers all four, or refuses.

WHAT THIS DELIBERATELY DOES NOT DO IS PICK A MODEL ON PRICE.

Model choice in this codebase is an evidence-based quality judgment, not an
optimisation. mail_classify.py excludes gpt-5-nano in a comment that records
why: measured on extraction-shaped work it fabricates, inventing 12 clearances
across 55 postings that never mention clearance and filling "none" wherever the
honest answer is "unstated". Nothing in a datasheet says that. A router that
minimised cost subject to declared capabilities would have routed that work
straight back to nano and undone the finding, silently, on a schedule.

So the caller supplies the models it has JUDGED FIT for its task, and this
chooses among only those - cheapest at the moment of the call, once every
declared constraint is satisfied. Where a caller names one model the result is
that model or an error, never a substitute. Quality stays a human judgment
recorded at the call site; capability, availability and price become checks.

There is no fallback chain, by design: two providers do not answer the same
prompt the same way, so a silent retry elsewhere makes a verdict whose author
depends on which call happened to fail. A call that cannot be satisfied raises.

WHAT A MODEL SWITCH ACTUALLY COSTS, since the obvious guess is wrong. It does
NOT fork the verdict log: model appears in no resolution key - jobs.py resolves
DISTINCT ON (url, check_type) and custom on (url, prompt_hash) - so the newest
row wins whatever produced it, and the board stays correct. The damage is a
bill. core/store.py's get_custom_result takes an optional `model` and
tasks/filters.py passes cfg.model to it as its skip-check, so the moment a
check is answered by a different model than last cycle, every verdict already
decided becomes invisible to that check and the sweep re-runs its whole
candidate set at full price - about $1.32 for the one enabled filter today,
$6.19 if all ten were live. Silent, on a schedule, with nothing in the log
saying why.

That is why candidates are the caller's to declare and why every call site
here names exactly one. A multi-model list is not free the moment it picks
differently twice, and the cost lands in the filter sweep rather than anywhere
near this file.

Key availability is deliberately NOT a resolution input - see server_key. What
a model can do is declared; whether this host holds a key for it is not, and a
resolver that needed one could not answer a question without spending money.

core does not import api. server_key reads the env var each provider declares,
which is why api.ai.server_key delegates here rather than keeping its own map.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from core import providers
from core.pricing import estimate_cost_usd, is_off_peak
from core.providers.spec import Model, StructuredOutput

# Weakest to strongest. StructuredOutput is a StrEnum with no inherent order,
# but the modes genuinely nest: anything that can enforce a schema can also
# return a bare object. A task states the WEAKEST mode it can live with.
_MODE_RANK = {
    StructuredOutput.NONE: 0,
    StructuredOutput.JSON_OBJECT: 1,
    StructuredOutput.JSON_SCHEMA: 2,
}


class NoEligibleModel(LookupError):
    """No candidate satisfied the task, with a per-candidate reason.

    Carries the reasons because the alternative is an operator staring at a
    task that will not run with nothing saying which requirement excluded which
    model - and "no eligible model" is true of a missing API key, an unpriced
    model and a provider with no batch lane alike.
    """


def server_key(provider: str) -> str:
    """The server's key for a provider, read from the env var it declares.

    Deliberately NOT part of resolve(). Whether a key happens to be present is
    a fact about this host's environment, not about what a model can do, and
    mixing the two would mean you cannot ask "what would this task use?"
    without one - which the effort walk needs to do, and which every test that
    does not make a real call needs to do. Call sites that are about to spend
    money check this; resolution answers the question it is named for.
    """
    known = providers.PROVIDERS.get(provider)
    return os.environ.get(known.api_key_env, "") if known else ""


class ModelChangeEffect(StrEnum):
    """What happens to work already done when this task's model changes.

    Declared rather than inferred, because it is a fact about the handler's
    candidate query and nothing here can see one. Getting it wrong on a screen
    is worse than omitting it: the two answers point in opposite directions.
    """

    # The sweep skips rows it has already done regardless of which model did
    # them, so a switch leaves the finished work alone and the catalog ends up
    # holding answers from both models. Nothing is re-paid; nothing is
    # reconciled either.
    MIXES = "mixes"
    # The sweep's skip-check is scoped by model, so the next cycle sees no
    # completed work and re-pays for all of it. tasks/filters.py is the case:
    # get_custom_result(url, prompt_hash, model=cfg.model).
    RERUNS = "reruns"


@dataclass(frozen=True)
class Evidence:
    """A measured finding about one model on this kind of work.

    Structured rather than prose because it has to reach a person overriding
    the choice from a screen, attached to the option it is about. A sentence in
    a code comment cannot do that, and a sentence hardcoded in the client rots
    the first time someone re-measures.

    sample_size is required, not garnish. "nano fabricated clearances" and
    "nano fabricated clearances in 12 of 55 postings" are different claims, and
    only the second can be argued with.
    """

    model: str
    # "excluded" or "chosen" - which way the finding cuts for this task.
    verdict: str
    finding: str
    sample_size: int
    measured_on: datetime.date


@dataclass(frozen=True)
class TaskShape:
    """What a piece of work needs, declared rather than implied.

    `candidates` is the caller's judgment about quality and is not optional:
    an empty tuple means nothing has been sanctioned, which is an error rather
    than an invitation to choose. One entry pins the model exactly, which is
    what every existing call site wants.
    """

    # The weakest structured-output mode the task can work with. Batched
    # extraction wants JSON_SCHEMA: the provider enforces the shape, so a bad
    # answer is a provider error rather than 20,000 rows of wrong keys.
    # The stable key for this task, and the same string the ledgers group by.
    # It lives on the declaration rather than being passed beside it so a
    # caller cannot name one purpose while running another's shape.
    purpose: str
    structured: StructuredOutput
    # True when the work is scheduled and can wait hours for a batch. This is a
    # HARD constraint, not a preference: submit_or_collect needs a provider
    # with a batch endpoint, and a provider without one cannot run the work at
    # all - it is not merely undiscounted.
    batched: bool
    max_output_tokens: int
    # Ranking only. The cheapest model is chosen using this as a stand-in for a
    # real request, so it needs to be the right order of magnitude rather than
    # exact - it never becomes a bill, which is written from real usage.
    est_prompt_tokens: int
    candidates: tuple[str, ...]
    # A named value must appear in the model's accepted set, and a model that
    # rejects it is not eligible - a batch submits whole and fails whole, so a
    # rejected parameter costs the entire run rather than one call.
    effort: str | None = None
    # Tried in order when `effort` is None: the first value this model actually
    # accepts wins. That is how "the cheapest thinking level this model will
    # take" gets expressed without a second table keyed by model name, which
    # would drift the moment a model is swapped. Empty falls through to the
    # model's own declared default.
    effort_preference: tuple[str, ...] = ()

    def resolved_effort(self) -> str | None:
        """The effort this shape would send to its single candidate.

        Only meaningful for a one-candidate shape, which is every shape here.
        Exists so a call site can ask what the walk decided without resolving
        the whole task - resolution needs a key present, and asking what effort
        a model takes should not.
        """
        declared = providers.model(self.candidates[0]) if self.candidates else None
        if declared is None:
            return None
        return self.effort or _preferred_effort(self, declared)

    params: dict[str, object] = field(default_factory=dict)
    # Human-readable label for the configuration screen.
    label: str = ""
    # The most requests one sweep of this task will make. Declared so a model
    # switch can be priced per cycle server-side rather than the client
    # multiplying a rate by a number it guessed - this repo's rule is that cost
    # comes from the server or nowhere. 0 means the sweep is unbounded and a
    # cycle cost genuinely cannot be projected, which the payload says rather
    # than papering over.
    per_cycle: int = 0
    # WHY these candidates and not others, in a sentence. This exists because
    # the reasoning is otherwise a code comment, and a person overriding the
    # choice from a UI cannot read code comments. mail_classify's exclusion of
    # gpt-5-nano - measured fabrication, 12 invented clearances across 55
    # postings - is exactly the sentence that has to reach the screen.
    notes: str = ""
    # What a model change does to work already finished. See ModelChangeEffect.
    on_model_change: ModelChangeEffect = ModelChangeEffect.MIXES
    # Measured findings, per model. Rendered beside the option each is about.
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Choice:
    """The resolved model, and the grounds for it."""

    provider: str
    model: str
    params: dict[str, object]
    est_cost_usd: Decimal | None
    off_peak: bool
    # Why this one rather than the others, for the log line at the call site.
    # A choice nobody can explain is a choice nobody can review.
    reason: str
    # True when a human picked this model instead of the ones the call site
    # sanctioned. Carried so the log line and the screen both say so - an
    # override that looks like a default is the failure mode here.
    overridden: bool = False


def _rejection(shape: TaskShape, name: str, declared: Model | None, provider: str | None) -> str:
    """Why this candidate cannot run this task, or "" when it can."""
    if declared is None or provider is None:
        return "not declared in any provider datasheet"
    if _MODE_RANK[declared.structured_output.mode] < _MODE_RANK[shape.structured]:
        return (
            f"declares {declared.structured_output.mode} output, "
            f"task needs at least {shape.structured}"
        )
    if shape.batched and providers.PROVIDERS[provider].batch_endpoint is None:
        # Not a missing discount - a missing capability. DeepSeek is the case:
        # both batch endpoints 404, so batched work cannot run there at all,
        # however cheap its off-peak window makes it.
        return "provider has no batch endpoint"
    if shape.effort is not None:
        accepts = declared.reasoning.accepts
        # An empty accepts tuple means nobody has enumerated the model's values.
        # Rejecting then would block a model that works, so the provider is
        # left to answer - the same rule _declared_efforts follows.
        if accepts and shape.effort not in accepts:
            return f"does not accept effort {shape.effort!r} (accepts {', '.join(accepts)})"
    if (
        declared.output.max_output_tokens is not None
        and shape.max_output_tokens > declared.output.max_output_tokens
    ):
        return (
            f"caps output at {declared.output.max_output_tokens} tokens, "
            f"task asks for {shape.max_output_tokens}"
        )
    if estimate_cost_usd(name, 1, 1) is None:
        # An unpriced model would make every call it served invisible to the
        # spend surfaces. None means "we do not know what this cost", and
        # routing work to it deliberately would manufacture that gap.
        return "has no published rates, so its spend could not be reported"
    return ""


def _preferred_effort(shape: TaskShape, declared: Model) -> str | None:
    """The first preferred effort this model accepts, else its own default.

    An empty `accepts` means nobody has enumerated the model's values. Falling
    through to the declared default there is deliberate: picking a preferred
    value the model may not take is how a whole batch dies on a 400, while the
    default is the value the datasheet says is safe.
    """
    if shape.effort_preference and declared.reasoning.accepts:
        for effort in shape.effort_preference:
            if effort in declared.reasoning.accepts:
                return effort
    return declared.reasoning.default


@dataclass(frozen=True)
class Candidacy:
    """Whether one model could run this task, and what it would cost.

    Built for the configuration screen, which must offer only models the
    task's declared needs admit - a model that cannot enforce a schema must
    not be selectable for a batched extraction, and the client should not have
    to re-derive that rule to know it.
    """

    model: str
    provider: str
    eligible: bool
    # Empty when eligible; otherwise the requirement that excluded it, in the
    # same words resolve() would have raised.
    rejection: str
    sanctioned: bool
    est_cost_usd: Decimal | None
    # per_cycle x the per-call cost, or None when the sweep is unbounded and a
    # cycle figure would be invented rather than computed.
    est_cycle_cost_usd: Decimal | None
    off_peak: bool


def candidates_for(shape: TaskShape, at: datetime.datetime | None = None) -> list[Candidacy]:
    """Every declared model, judged against this task.

    Returns the whole catalogue rather than only the eligible ones, because a
    screen offering a short list with no explanation is how someone concludes
    the missing model is a bug. The rejection reason is the useful half.

    ORDERED HERE, cheapest eligible first, then the ineligible alphabetically.
    The order is the server's job because the prices are Decimals rendered as
    strings - a client sorting those lexicographically puts "9.00" above
    "10.00", and a client parsing them into floats to sort has rounded money
    to avoid it. Sending them in the order they should be shown removes the
    choice.
    """
    out: list[Candidacy] = []
    for name, (provider, declared) in sorted(providers.MODELS.items()):
        if not declared.selectable:
            continue
        why = _rejection(shape, name, declared, provider)
        per_call = (
            None
            if why
            else estimate_cost_usd(
                name,
                shape.est_prompt_tokens,
                shape.max_output_tokens,
                batched=shape.batched,
                at=at,
            )
        )
        out.append(
            Candidacy(
                model=name,
                provider=provider,
                eligible=not why,
                rejection=why,
                sanctioned=name in shape.candidates,
                est_cost_usd=per_call,
                est_cycle_cost_usd=(
                    per_call * shape.per_cycle if per_call is not None and shape.per_cycle else None
                ),
                off_peak=is_off_peak(declared.rates, at),
            )
        )
    # None sorts last among the eligible, which cannot happen - _rejection
    # already refuses an unpriced model - but the key must total-order anyway.
    return sorted(
        out,
        key=lambda c: (
            not c.eligible,
            c.est_cost_usd if c.est_cost_usd is not None else Decimal("Infinity"),
            c.model,
        ),
    )


def _params_for(shape: TaskShape, declared: Model) -> dict[str, object]:
    """The request params this shape implies for this model.

    Shared by the resolved path and the override path so a configured model
    cannot end up with different effort handling than a sanctioned one - which
    would make an override change two things when the person changed one.
    """
    params = dict(shape.params)
    if declared.reasoning.param:
        effort = shape.effort or _preferred_effort(shape, declared)
        if effort:
            params.setdefault(declared.reasoning.param, effort)
    params.setdefault("max_output_tokens", shape.max_output_tokens)
    return params


def resolve(
    shape: TaskShape,
    at: datetime.datetime | None = None,
    override: str | None = None,
) -> Choice:
    """The cheapest sanctioned model that can actually do the work.

    `at` is the moment the work runs, passed through to pricing because one
    provider's rates depend on it: DeepSeek charges half outside its peak
    windows, ~79% of the week, and that discount applies to synchronous calls
    where a human is waiting - the traffic a batch lane can never help. Omitting
    `at` prices at peak, which is the same overstate-rather-than-guess rule
    pricing uses everywhere.
    """
    if not shape.candidates:
        raise NoEligibleModel("no candidate models were declared for this task")

    # An override replaces the caller's judgment, deliberately - the person who
    # owns the system may overrule a call site. It does NOT replace the task's
    # declared needs: a model that cannot do the work would fail at the
    # provider, mid-batch, having already been paid for. Capability stays hard,
    # sanction becomes soft, and the difference is the whole design.
    if override:
        declared = providers.model(override)
        provider = providers.provider_of(override)
        why = _rejection(shape, override, declared, provider)
        if why:
            raise NoEligibleModel(f"{override} cannot run this task - {why}")
        assert provider is not None and declared is not None
        cost = estimate_cost_usd(
            override,
            shape.est_prompt_tokens,
            shape.max_output_tokens,
            batched=shape.batched,
            at=at,
        )
        sanctioned = override in shape.candidates
        return Choice(
            provider=provider,
            model=override,
            params=_params_for(shape, declared),
            est_cost_usd=cost,
            off_peak=is_off_peak(declared.rates, at),
            reason=(
                "configured override"
                if sanctioned
                else "configured override, outside the models this call site sanctioned"
            ),
            overridden=not sanctioned,
        )

    eligible: list[tuple[Decimal, str, str, bool]] = []
    rejected: list[str] = []
    for name in shape.candidates:
        declared = providers.model(name)
        provider = providers.provider_of(name)
        why = _rejection(shape, name, declared, provider)
        if why:
            rejected.append(f"{name}: {why}")
            continue
        assert provider is not None and declared is not None
        cost = estimate_cost_usd(
            name,
            shape.est_prompt_tokens,
            shape.max_output_tokens,
            batched=shape.batched,
            at=at,
        )
        # _rejection has already refused anything unpriced, so a None here
        # would mean the two disagree about what "priced" means.
        assert cost is not None
        eligible.append((cost, name, provider, is_off_peak(declared.rates, at)))

    if not eligible:
        raise NoEligibleModel("no declared model can run this task - " + "; ".join(rejected))

    # Ties keep the caller's own ordering, which is the only ordering that
    # encodes their judgment. sorted() is stable, so an equal-priced later
    # candidate never displaces an earlier one.
    cost, name, provider, off_peak = min(
        eligible, key=lambda e: e[0] if e[0] is not None else Decimal("Infinity")
    )
    declared = providers.model(name)
    assert declared is not None
    params = _params_for(shape, declared)

    if len(shape.candidates) == 1:
        reason = "the only model the caller sanctioned"
    else:
        others = ", ".join(n for _, n, _, _ in eligible if n != name)
        reason = f"cheapest of {len(eligible)} eligible ({others}) at the time of the call"
    if off_peak:
        reason += "; off-peak rate applies"
    if rejected:
        reason += f"; excluded {len(rejected)}"
    return Choice(
        provider=provider,
        model=name,
        params=params,
        est_cost_usd=cost,
        off_peak=off_peak,
        reason=reason,
    )
