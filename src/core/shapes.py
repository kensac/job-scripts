"""Task declarations shared by handlers, routing, and fleet pricing.

SHAPES declares each task's purpose, candidate models, output cap, token
estimate, cycle size, and supporting evidence. Declared cycle sizes seed
app_config; handlers and the fleet budget read persisted overrides via
api.task_config.configured_shape."""

from __future__ import annotations

import datetime
import os

from core.answers import VERIFICATION_REQUEST
from core.batch import BATCH_TOKEN_BUDGET, BATCH_WAVE_CONCURRENCY
from core.providers.spec import StructuredOutput
from core.routing import Evidence, TaskShape

# --- compensation extraction ---

# Default for comp_extract_per_cycle: ~6.5k tokens/spec against 1.8M/wave (~276/wave)
# across BATCH_WAVE_CONCURRENCY concurrent waves.
EXTRACT_COMP_PER_CYCLE = 1100


COMP_TASK = TaskShape(
    purpose="comp",
    label="Compensation extraction",
    per_cycle=EXTRACT_COMP_PER_CYCLE,
    notes=(
        "Copies stated compensation from the posting and leaves absent figures unknown. "
        "The model upgrade preserves low reasoning, the output cap and batching. "
        "Previously extracted results are retained; comparative quality is unmeasured."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=1500,
    est_prompt_tokens=6500,
    effort="low",
    candidates=("gpt-6-luna",),
)


# --- requirements extraction ---

REQUIREMENTS_MODEL = os.environ.get("JOBTRACKER_REQUIREMENTS_MODEL", "gpt-5.6-luna")

# Cheapest accepted effort first so model swaps pick a supported value.
_REQUIREMENTS_EFFORT_PREFERENCE = ("none", "minimal", "low")

# Pilot p100 output was 378 tokens (up to 802 reasoning tokens at low effort);
# 2000 leaves headroom so responses do not truncate mid-JSON and re-queue.
REQUIREMENTS_MAX_OUTPUT_TOKENS = 2000

# Default for requirements_extract_per_cycle: ~3,304 estimated tokens/spec
# against 1.8M BATCH_TOKEN_BUDGET (~545/wave) x 4 waves = 2,179.
EXTRACT_REQUIREMENTS_PER_CYCLE = 2179


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
        "quality here has not been audited the way the other two were. "
        "This task did NOT move to gpt-6-luna with the rest of the tasks on "
        "2026-09-22: the model here was chosen on a failure rate measured "
        "over 60,000 batched requests, and the newer model has no such "
        "record. JOBTRACKER_REQUIREMENTS_MODEL moves it once one exists."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=REQUIREMENTS_MAX_OUTPUT_TOKENS,
    # Fitted against a 60-posting pilot (1,255 fixed + 0.181/char at ~5.5 chars/token).
    est_prompt_tokens=2270,
    # A preference, not a pin: luna rejects "minimal" and mini rejects "none", so a
    # pinned effort fails a swapped model's whole batch on a 400 (#179).
    effort_preference=_REQUIREMENTS_EFFORT_PREFERENCE,
    candidates=(REQUIREMENTS_MODEL,),
)


# --- location classification ---

# Default for classify_locations_per_cycle. Strings are short, so the whole backlog
# (8,735 distinct strings on 2026-09-04) fits one cycle.
CLASSIFY_LOCATIONS_PER_CYCLE = 10000

LOCATIONS_TASK = TaskShape(
    purpose="locations",
    label="Location classification",
    per_cycle=CLASSIFY_LOCATIONS_PER_CYCLE,
    notes=(
        "Naming the country, state and city a short string refers to is a "
        "lookup, not a judgment about silence. "
        "A string that names no single place is left empty, which excludes "
        "nothing, so the cost of a wrong answer is one visible posting."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    # Room for ~32 places at ~64 tokens each without truncating multi-city strings.
    max_output_tokens=2048,
    est_prompt_tokens=260,
    effort_preference=("minimal", "low"),
    candidates=("gpt-6-luna",),
)


# --- closed and clearance verification ---

VERIFY_TASK = TaskShape(
    purpose="verify",
    label="Closed and clearance verification",
    notes=(
        "A yes/no read of whether a posting is still open and whether it "
        "demands a clearance. The model upgrade preserves low reasoning, batching "
        "and the existing eligibility and re-verification schedule. Existing paid "
        "batches keep their original model provenance."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=VERIFICATION_REQUEST.max_output_tokens,
    est_prompt_tokens=5500,
    effort="low",
    candidates=("gpt-6-luna",),
)


# --- mail classification ---

# One model for both: the backfill ran on luna, and an ongoing feed on another model
# would read the same mail by different standards, shifting the funnel unexplained.
# Separate env overrides so backfill and ongoing can be moved independently.
BACKFILL_MODEL = os.environ.get("JOBTRACKER_MAIL_BACKFILL_MODEL", "gpt-6-luna")
ONGOING_MODEL = os.environ.get("JOBTRACKER_MAIL_ONGOING_MODEL", "gpt-6-luna")

# Cheapest accepted effort first; accepted sets live in core/providers/. A shared
# constant 400'd every ongoing call; luna at "none" measured ~40 output tokens/message.
_CLASSIFY_EFFORT_PREFERENCE = ("none", "minimal", "low")

# The schema's few short fields with no reasoning; a smaller cap truncates the JSON.
CLASSIFY_MAX_TOKENS = 400

# Default for mail_classify_per_cycle, sized to fill BATCH_WAVE_CONCURRENCY waves.
# Measured from ai_batches.est_tokens (270 batches, 89,413 requests, 2026-09-02 to 09-13).
CLASSIFY_SPEC_EST_TOKENS = 2162
CLASSIFY_PER_CYCLE = BATCH_WAVE_CONCURRENCY * BATCH_TOKEN_BUDGET // CLASSIFY_SPEC_EST_TOKENS


def _classify_task(model: str, purpose: str, label: str) -> TaskShape:
    """Single-candidate classification shape pinned by quality evidence."""
    return TaskShape(
        purpose=purpose,
        label=label,
        per_cycle=CLASSIFY_PER_CYCLE,
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
        # Mean billed input tokens over 84,345 completed requests (2026-09-02 to 09-13).
        est_prompt_tokens=2580,
        effort_preference=_CLASSIFY_EFFORT_PREFERENCE,
        candidates=(model,),
    )


BACKFILL_TASK = _classify_task(
    BACKFILL_MODEL, "mail_classify_backfill", "Mail classification (backfill)"
)
ONGOING_TASK = _classify_task(ONGOING_MODEL, "mail_classify", "Mail classification (ongoing)")


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
        "so the better writer is the choice. The default moved to gpt-6-luna on 2026-09-22 "
        "for price; that comparison is of its predecessor and has not been repeated."
    ),
    structured=StructuredOutput.JSON_SCHEMA,
    batched=True,
    max_output_tokens=1200,
    est_prompt_tokens=3500,
    effort_preference=("none", "minimal", "low"),
    candidates=("gpt-6-luna",),
)


JOB_PROFILE_TASK = TaskShape(
    purpose="job_profile",
    label="Job profile shadow classification",
    # Default for job_profiles_per_cycle in app_config.
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
    candidates=("gpt-6-luna",),
)


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
