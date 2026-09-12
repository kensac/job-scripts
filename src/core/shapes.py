"""What every configurable task declares it needs, and the registry of them.

These are declarations, not behaviour: a purpose, the models the call site has
judged fit, an output cap, a token estimate, how much one cycle does, and the
measured evidence behind the choice. Nothing here runs a task.

They live below the handlers because both layers read them and only one layer
runs them. `api.budget` prices a fleet cycle from SHAPES and the configuration
screen offers what SHAPES holds, so while these were declared inside the
handler modules the services had to import the handlers to read a constant.
That was five of the nine exceptions on the import contract.

SHAPES cannot be a registry that the handlers write into on import. A registry
would be empty for anything that has not imported `tasks`, which is exactly the
caller this move exists for, and the failure would be a fleet cost silently
computed over no tasks rather than an error.

The constants a shape reads came with it: the model, the output cap, the effort
preference and the per-cycle size ARE the declaration. The handler imports them
back for its own SQL, so a cap still has one definition.
"""

from __future__ import annotations

import datetime
import os

from core.answers import VERIFICATION_REQUEST
from core.providers.spec import StructuredOutput
from core.routing import Evidence, TaskShape

# Shared by the re-verification sweep and its pre-cutover shadow report. The
# report compares the full stale population and carries the ordinary cycle cap
# separately, so a cap cannot make two different populations appear equal.
REVERIFY_DAYS = int(os.environ.get("JOBTRACKER_REVERIFY_DAYS", "7"))
REVERIFY_PER_CYCLE = int(os.environ.get("JOBTRACKER_REVERIFY_PER_CYCLE", "0"))

# --- compensation extraction ---

# Comp extraction runs hourly and each pass is bounded, so one task cannot pull
# the whole catalog into memory or occupy a worker indefinitely. The size is
# chosen to fill the batch-wave concurrency rather than picked arbitrarily: a
# comp spec is ~6.5k tokens against a 1.8M-token wave budget, so ~276 specs per
# wave, and waves now run BATCH_WAVE_CONCURRENCY at a time.
EXTRACT_COMP_PER_CYCLE = int(os.environ.get("JOBTRACKER_EXTRACT_COMP_PER_CYCLE", "1100"))


# What this work needs, rather than which model happens to serve it. One
# candidate, so the resolved model is gpt-5-nano exactly as before - what
# changes is that the capability, the key and the price are now checked
# instead of assumed. Comp extraction is the shape nano handles well: a
# stated number copied off the page, not a judgment about silence.
COMP_TASK = TaskShape(
    purpose="comp",
    label="Compensation extraction",
    per_cycle=EXTRACT_COMP_PER_CYCLE,
    notes=(
        "Copying a stated number off the page, which is the shape gpt-5-nano "
        "handles well - it is not asked to judge silence, only to read a figure "
        "that is either printed or absent."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=1500,
    est_prompt_tokens=6500,
    effort="low",
    candidates=("gpt-5-nano",),
)


# --- requirements extraction ---

# gpt-5-nano is the fleet default and is the wrong model here, so this pass
# names its own. Audited over 60 real postings against whether the page even
# mentions the fact: nano at "low" effort invented a clearance level for 12 of
# 55 postings that never mention clearance, and lost 5 of 60 responses to the
# output cap; nano at "minimal" filled 0 and "none" wherever the honest answer
# was "unstated", which is the one distinction this schema exists to keep.
# gpt-5-mini at "minimal" invented one degree, one yoe and no clearances, and
# truncated nothing. Batched over the whole corpus that is $9.89 against nano's
# $1.98 - a one-time pass over postings that can never be re-scraped.
REQUIREMENTS_MODEL = os.environ.get("JOBTRACKER_REQUIREMENTS_MODEL", "gpt-5.6-luna")


# Cheapest first, and every value here is accepted by SOME model in the
# registry, so swapping the model swaps the effort with it rather than sending
# one the model refuses. luna takes "none"; nano takes "minimal", and sent
# the same request directly on 2026-09-05 it completed at ~200 output tokens
# with no reasoning, against 750 to 1,450 at "low". The 21,525-line failure
# of 2026-09-04 was not the effort: OpenAI's error file for those batches says
# every line carried "none", which nano rejects, from a routing path that did
# not yet re-pick the effort for an overridden model.
_REQUIREMENTS_EFFORT_PREFERENCE = ("none", "minimal", "low")


# Measured p100 over the pilot was 378 tokens; the JSON's whole variable part is
# the two skill arrays, so the budget is set to carry roughly three times the
# widest skill list seen. An unfinished response is unparseable JSON, and an
# unparseable line leaves the url unextracted for the next sweep to re-pay
# forever, so the headroom is worth more than the tokens - a cap is a ceiling,
# not a charge. 2000 rather than 1000 because nano at low effort spent 802
# output tokens a posting in the pilot before the JSON, and a cap the
# reasoning alone can hit turns every long posting into an unparseable line.
REQUIREMENTS_MAX_OUTPUT_TOKENS = 2000


# Bounded per cycle for the same reason comp extraction is: one task must not
# pull the whole corpus into memory or hold a worker indefinitely. Sized to fill
# the batch-wave concurrency rather than picked round, using core.batch's own
# estimator since that is what actually chunks the waves: instructions (3,611
# chars) plus a posting (5,608 chars, the corpus mean after truncation) is 2,302
# tokens at BATCH_CHARS_PER_TOKEN, plus REQUIREMENTS_MAX_OUTPUT_TOKENS reserved,
# so 3,304 tokens a spec against the 1.8M-token BATCH_TOKEN_BUDGET is 545 specs
# per wave, and waves run BATCH_WAVE_CONCURRENCY (4) at a time: 2,179.
#
# That estimate is deliberately conservative, and it is worth knowing by how
# much. Fitted against real API usage over a 60-posting pilot, the true cost of
# a request is 1,255 + 0.181 x content_chars input tokens: job postings tokenize
# at about 5.5 characters per token, not the 4 that BATCH_CHARS_PER_TOKEN
# assumes, and the 1,255 fixed is the instructions plus the JSON schema, which
# is roughly 45% of a request's input at this posting length. So real waves run
# under budget rather than over it, which is the safe direction. Whole corpus:
# 47.0M input and 4.0M output tokens, $9.89 batched at REQUIREMENTS_MODEL.
EXTRACT_REQUIREMENTS_PER_CYCLE = int(
    os.environ.get("JOBTRACKER_EXTRACT_REQUIREMENTS_PER_CYCLE", "2179")
)


# The same declaration every other batched extraction makes. The model is the
# caller's judgment - the note above is why mini and not nano - and the router
# checks it can do what this task needs rather than choosing it for cost.
# est_prompt_tokens is the fitted figure from the pilot below, not the
# conservative chunking estimate: it ranks candidates and never becomes a bill.
REQUIREMENTS_TASK = TaskShape(
    purpose="requirements",
    label="Requirements extraction",
    per_cycle=EXTRACT_REQUIREMENTS_PER_CYCLE,
    evidence=(
        Evidence(
            model="gpt-5-nano",
            verdict="excluded",
            finding=(
                "Invented a clearance level for 12 of 55 postings whose page "
                "never mentions clearance, and at minimal effort filled 0 and "
                "'none' wherever the honest answer was 'unstated' - which is "
                "the distinction this extraction exists to keep."
            ),
            sample_size=60,
            measured_on=datetime.date(2026, 9, 2),
        ),
        Evidence(
            model="gpt-5-mini",
            verdict="excluded",
            finding=(
                "Extracts well - over the same postings it invented one "
                "degree, one years-of-experience figure and no clearances, "
                "and truncated nothing. Excluded on RELIABILITY rather than "
                "quality: 499 of its 31,999 batched requests failed, against "
                "0 of 19,971 on nano and 0 of 60,000 on luna. A failed line "
                "leaves a posting unextracted and looks like a batch that "
                "worked, so nothing was watching."
            ),
            sample_size=31999,
            measured_on=datetime.date(2026, 9, 2),
        ),
        Evidence(
            model="gpt-5.6-luna",
            verdict="chosen",
            finding=(
                "Zero failures across 60,000 batched requests, the only model "
                "with a clean record at that volume. Chosen for reliability; "
                "its extraction quality on this task has NOT been audited the "
                "way nano and mini were, and that gap is the reason this entry "
                "exists rather than a note."
            ),
            sample_size=60000,
            measured_on=datetime.date(2026, 9, 2),
        ),
    ),
    notes=(
        "Not nano, on measured quality: over 60 real postings it invented a "
        "clearance level for 12 of 55 that never mention clearance, and at "
        "minimal effort filled 0 and 'none' wherever the honest answer was "
        "'unstated' - the one distinction this extraction exists to keep. "
        "Not mini, on measured RELIABILITY: 499 of 31,999 batched requests "
        "failed against zero on the other two, and a failed line leaves a "
        "posting unextracted while looking like a batch that worked. luna is "
        "the only model with a clean record at volume, and its extraction "
        "quality here has not been audited the way the other two were."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=REQUIREMENTS_MAX_OUTPUT_TOKENS,
    est_prompt_tokens=2270,
    # Preference, not a pin. luna REJECTS "minimal" and mini rejects "none",
    # so a literal effort makes the model unswappable: point this at the other
    # one and resolve() refuses, or worse a batch submits and fails whole on a
    # 400. That is #179 exactly. The model picks the first value it accepts.
    effort_preference=_REQUIREMENTS_EFFORT_PREFERENCE,
    candidates=(REQUIREMENTS_MODEL,),
)


# --- location classification ---

# Strings are short and the answer is a lookup the model already knows, so the
# whole backlog (8,735 distinct strings on 2026-09-04) fits one cycle; after
# that a cycle carries only the strings new boards wrote since the last one.
# Persisted config (classify_locations_per_cycle), so the first pass can be
# a small sample read off GET /admin/locations before the backlog is paid for.
CLASSIFY_LOCATIONS_PER_CYCLE = 10000

LOCATIONS_TASK = TaskShape(
    purpose="locations",
    label="Location classification",
    per_cycle=CLASSIFY_LOCATIONS_PER_CYCLE,
    notes=(
        "Naming the country, state and city a short string refers to is a "
        "lookup, not a judgment about silence: the shape gpt-5-nano handles. "
        "A string that names no single place is left empty, which excludes "
        "nothing, so the cost of a wrong answer is one visible posting."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=120,
    est_prompt_tokens=260,
    effort_preference=("minimal", "low"),
    candidates=("gpt-5-nano",),
)


# --- closed and clearance verification ---

# One candidate, so this resolves to gpt-5-nano exactly as it did when the name
# was written inline - the change is that a missing key or a model that cannot
# enforce a schema fails at resolution, with a reason, instead of at the
# provider after a wave has been built.
#
# Deliberately NOT widened to a second model. tasks/filters.py scopes its
# cached-verdict check by model, so a sweep that answered on a different model
# than last cycle would see no cached verdicts and re-run everything at full
# price. See core/routing.py.
VERIFY_TASK = TaskShape(
    purpose="verify",
    label="Closed and clearance verification",
    notes=(
        "A yes/no read of whether a posting is still open and whether it "
        "demands a clearance. Cheap and high volume - every active job, every "
        "cycle - so the fleet default is the right place to start."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=VERIFICATION_REQUEST.max_output_tokens,
    est_prompt_tokens=5500,
    effort="low",
    candidates=("gpt-5-nano",),
)


# --- mail classification ---

# The one-time historical sweep and the ongoing trickle are priced differently
# enough to be different models, and neither is the fleet default.
#
# gpt-5-nano is excluded on evidence rather than price: measured on
# extraction-shaped work it FABRICATES, inventing 12 clearances across 55
# postings and filling 0/"none" wherever the true answer is "unstated". This is
# the same shape - a deadline that was never stated must not become a guessed
# date, and silence must not become a fabricated rejection.
#
# Measured over the 38,685-message mailbox, batched: luna $10.44, mini $14.99.
#
# ONE model for both paths, on Kanishk's call, and the reason is consistency
# rather than the $4.55. The backfill classified 67k messages on luna; an
# ongoing feed on a different model reads the same mail by different standards,
# so a rejection recognised in the archive might not be recognised next week -
# and the difference would show up as a change in the funnel that nothing in
# the funnel explains.
#
# The two constants stay separate because their ENV OVERRIDES are separate: the
# per-task model config can move one path without the other, which is the point
# of that feature. They simply default to the same model now.
BACKFILL_MODEL = os.environ.get("JOBTRACKER_MAIL_BACKFILL_MODEL", "gpt-5.6-luna")
ONGOING_MODEL = os.environ.get("JOBTRACKER_MAIL_ONGOING_MODEL", "gpt-5.6-luna")


# A backfill may ask for more, because it is a ONE-TIME sweep over a mailbox
# rather than an hourly trickle: at the ongoing cap, 34,000 archived messages
# take about 28 hours of cycles to work through.
#
# The ceiling is derived from what a wave can actually carry rather than
# picked: core.batch budgets BATCH_TOKEN_BUDGET tokens per wave and runs
# BATCH_WAVE_CONCURRENCY waves at once, and a classification spec is ~1,500
# tokens (measured on real mail, not estimated). That is ~1,200 specs per wave
# and ~4,800 in flight, so asking for much beyond that only queues work the
# provider will not start any sooner.
MAX_CLASSIFY_PER_CYCLE = int(os.environ.get("JOBTRACKER_MAIL_CLASSIFY_MAX", "5000"))


# Reasoning effort is PER MODEL, because these two do not accept the same
# values. Probed against the live APIs, which name the sets in their 400s:
#
#   gpt-5-mini    accepts minimal, low, medium, high   REJECTS none
#   gpt-5.6-luna  accepts none, low, medium, high,     REJECTS minimal
#                         xhigh, max
#
# The intersection is only {low, medium, high}, so a single shared constant
# would have to give up the cheapest setting on both. Each gets its cheapest
# accepted value instead: classification is a labelling task that gains
# nothing from reasoning, and a dry run measured ~40 output tokens per message
# at luna/none against the ~200 assumed - most of why the corpus estimate fell
# from $10.44 to $7.35.
#
# A shared constant is what shipped first, and it 400'd on every ongoing call
# while backfill worked, because the value chosen suited only the model that
# had been dry-run by hand.
#
# Which value each model accepts is NOT restated here. It is declared in
# core/providers/, the model picks the first of these it accepts, and a second
# copy keyed by model name would drift the moment a model is swapped by env
# var - which both model constants above can be.
_CLASSIFY_EFFORT_PREFERENCE = ("none", "minimal", "low")


# Every model in the intersection above, so a model the registry has not been
# taught still gets a value both generations accept rather than failing the
# whole batch. Deliberately not the cheapest: guessing cheap at an unknown
# model is how the 400 happened.
FALLBACK_EFFORT = "low"


# Enough for the schema's handful of short fields. The model does not reason
# here, so a larger ceiling buys nothing and a smaller one truncates JSON
# mid-string, which arrives as an unparsable line rather than an error.
CLASSIFY_MAX_TOKENS = 400


def _classify_task(model: str, purpose: str, label: str) -> TaskShape:
    """One model per shape, never a list.

    The choice above is an evidence judgment, not an optimisation: a router
    minimising cost subject to declared capability would pick nano and reinstate
    exactly the fabrication these two models were chosen to avoid. Resolution
    still earns its place - it checks the key, the schema capability and the
    price, and it is where the effort walk happens.
    """
    return TaskShape(
        purpose=purpose,
        label=label,
        per_cycle=MAX_CLASSIFY_PER_CYCLE,
        evidence=(
            Evidence(
                model="gpt-5-nano",
                verdict="excluded",
                finding=(
                    "Invented a clearance level for 12 of 55 postings whose page "
                    "never mentions clearance, and at minimal effort filled 0 and "
                    "'none' wherever the honest answer was 'unstated' - which is "
                    "the distinction this extraction exists to keep."
                ),
                sample_size=60,
                measured_on=datetime.date(2026, 9, 2),
            ),
        ),
        # The comment that used to live above BACKFILL_MODEL, promoted to data
        # so it reaches a person overriding this from a screen. A code comment
        # cannot warn the one reader who most needs the warning.
        notes=(
            "gpt-5-nano is excluded on evidence rather than price: measured on "
            "extraction-shaped work it fabricates, inventing 12 clearances "
            "across 55 postings and filling 0/'none' wherever the true answer "
            "is 'unstated'. The same shape applies here - a deadline that was "
            "never stated must not become a guessed date, and silence must not "
            "become a fabricated rejection. Backfill and ongoing are priced "
            "differently enough to be different models: over the 38,685-message "
            "mailbox, batched, luna is $10.44 and mini $14.99, while ongoing at "
            "~80/day is $11.31/yr on mini where per-message quality matters more."
        ),
        structured=StructuredOutput.JSON_SCHEMA,
        batched=True,
        max_output_tokens=CLASSIFY_MAX_TOKENS,
        # Ranking only, and only ever against itself here, since there is one
        # candidate. The real spec size is ~6k tokens.
        est_prompt_tokens=6000,
        effort_preference=_CLASSIFY_EFFORT_PREFERENCE,
        candidates=(model,),
    )


BACKFILL_TASK = _classify_task(
    BACKFILL_MODEL, "mail_classify_backfill", "Mail classification (backfill)"
)
ONGOING_TASK = _classify_task(ONGOING_MODEL, "mail_classify", "Mail classification (ongoing)")


def effort_for(model: str) -> str:
    """The cheapest reasoning effort this model actually accepts.

    Unknown models get the intersection value rather than a guess: a batch
    submits whole and fails whole, so a rejected parameter costs the entire
    run, not one call.

    The choosing itself now lives in core.routing, which every task resolves
    through; this keeps the name and the unknown-model floor that callers here
    rely on, without a second copy of the preference walk.
    """
    return _classify_task(model, "effort_probe", "").resolved_effort() or FALLBACK_EFFORT


# --- application answers ---

APPLICATION_TASK = TaskShape(
    purpose="application",
    label="Application answers",
    per_cycle=0,
    notes=(
        "Prose a recruiter reads, written from a resume the model must not embroider. "
        "Measured 2026-09-06 over four live questions: gpt-5.6-luna without reasoning picked "
        "the resume facts that fit the role and stayed inside them; gpt-5-nano padded with "
        "generic openers and loosened one claim. Both cost under a tenth of a cent an answer, "
        "so the better writer is the choice."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=1200,
    est_prompt_tokens=3500,
    effort_preference=("none", "minimal", "low"),
    candidates=("gpt-5.6-luna",),
)


JOB_PROFILE_TASK = TaskShape(
    purpose="job_profile",
    label="Job profile shadow classification",
    per_cycle=500,
    notes=(
        "A shadow-only compact taxonomy used to compare general job classifications. "
        "It does not participate in filtering or visibility."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=1000,
    est_prompt_tokens=3200,
    effort_preference=("none",),
    candidates=("gpt-5.6-luna",),
)


# Every configurable task, keyed by the purpose its own shape declares. This
# is the list the configuration screen offers and the list resolve() is asked
# about - one registry, so a task cannot be configurable but unreported, or
# reported under a name nothing configures.
SHAPES = {
    shape.purpose: shape
    for shape in (
        COMP_TASK,
        REQUIREMENTS_TASK,
        LOCATIONS_TASK,
        VERIFY_TASK,
        BACKFILL_TASK,
        ONGOING_TASK,
        APPLICATION_TASK,
        JOB_PROFILE_TASK,
    )
}
