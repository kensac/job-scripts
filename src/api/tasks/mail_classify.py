"""Turning stored messages into application events.

Every message is classified. The prefilter orders this sweep but gates
nothing, because a filtered-out email is the one unrecoverable failure in this
pipeline: the posting is closed, the thread is not coming back, and no re-run
recovers it.

The output is an append-only event log, latest row per (message, kind) winning,
matching the verdict log. Re-classifying appends rather than destroying what a
previous pass concluded.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
from typing import Any, Literal

from pydantic import BaseModel

from api import db
from api.tasks.runtime import _batch_event_hook, _set_progress, submit_or_collect

logger = logging.getLogger("jobtracker_worker")

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
# Ongoing at ~80/day is $11.31/yr on mini, where the per-message quality
# matters more than the total.
BACKFILL_MODEL = os.environ.get("JOBTRACKER_MAIL_BACKFILL_MODEL", "gpt-5.6-luna")
ONGOING_MODEL = os.environ.get("JOBTRACKER_MAIL_ONGOING_MODEL", "gpt-5-mini")

# A classification spec is the instructions plus a body capped at 20k chars,
# so ~6k tokens against the same 1.8M-token wave budget comp.py sizes against:
# ~300 specs per wave, times BATCH_WAVE_CONCURRENCY waves in flight.
CLASSIFY_PER_CYCLE = int(os.environ.get("JOBTRACKER_MAIL_CLASSIFY_PER_CYCLE", "1200"))

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
# values. Probed against the live APIs:
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
_EFFORT_BY_MODEL = {
    "gpt-5.6-luna": "none",
    "gpt-5-mini": "minimal",
}

# Every model in the intersection above, so an unconfigured model still gets a
# value both generations accept rather than failing the whole batch.
FALLBACK_EFFORT = "low"

# Enough for the schema's handful of short fields. The model does not reason
# here, so a larger ceiling buys nothing and a smaller one truncates JSON
# mid-string, which arrives as an unparsable line rather than an error.
CLASSIFY_MAX_TOKENS = 400

EVENT_KINDS = (
    "acknowledgement",
    "rejection",
    "assessment_invite",
    "interview_invite",
    "interview_scheduled",
    "info_request",
    "offer",
    "recruiter_outreach",
    "position_closed",
    "not_job_related",
)

_INSTRUCTIONS = """You classify a single email from a job seeker's mailbox.

Return the event kind that best describes what the SENDER is communicating.
Use "not_job_related" for anything that is not about a job application or a
recruiter approaching the candidate - newsletters, bank statements, receipts,
code review notifications, and so on. Most mail is not job related.

"recruiter_outreach" is cold contact about a role the candidate did not apply
to. It is NOT an update on an existing application.

For company and role, copy what the email states. If the email does not state
it, leave it null. Do NOT infer a company from the sender's domain and do NOT
guess a role title.

For a deadline, only give a date the email actually states or plainly implies
("within 5 days" from a dated email is implied; "soon" is not). Leave it null
otherwise. Never invent one.
"""


class MailClassification(BaseModel):
    kind: Literal[
        "acknowledgement",
        "rejection",
        "assessment_invite",
        "interview_invite",
        "interview_scheduled",
        "info_request",
        "offer",
        "recruiter_outreach",
        "position_closed",
        "not_job_related",
    ]
    # Null means the email did not say. That is a different fact from "no
    # company", and collapsing the two is what makes a model's output
    # unusable for matching.
    company: str | None
    role_title: str | None
    # ISO date, or null. Never a guess.
    deadline: str | None
    deadline_is_explicit: bool
    confidence: Literal["high", "medium", "low"]


def effort_for(model: str) -> str:
    """The cheapest reasoning effort this model actually accepts.

    Unknown models get the intersection value rather than a guess: a batch
    submits whole and fails whole, so a rejected parameter costs the entire
    run, not one call.
    """
    return _EFFORT_BY_MODEL.get(model, FALLBACK_EFFORT)


# Dates the model actually returns, measured over 200 real messages rather
# than guessed: "March 1, 2023", "Thursday, February 1, 2024",
# "Tue Dec 10, 2024 at 4:00PM (EST)". The instruction asks for ISO and the
# model frequently answers in prose, so accepting only ISO discarded 7.5% of
# the deadlines it found - real dates, thrown away.
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        start=1,
    )
}
_MONTHS.update({m[:3]: i for m, i in _MONTHS.items()})

_DATE_RE = re.compile(
    r"\b(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:\s*,)?(?:\s+(?P<year>\d{4}))?\b"
)


def parse_deadline(value: str | None, *, sent_at: datetime.datetime | None = None):
    """An exact instant, or nothing - plus whether the year had to be inferred.

    Returns (deadline, inferred). Three outcomes, deliberately distinct:

    ISO or a full prose date resolves exactly.

    A date with no year ("Jan. 15") resolves against the message it came from
    and is marked INFERRED, because that is a real inference and the schema
    already carries a flag for exactly this. Without a message date it is
    dropped rather than guessed against today.

    Anything with no resolvable date at all ("Tuesday at 2:00 PM", "soon",
    "2022-09-??") becomes nothing. A partial date is not a deadline, and
    inventing a day to make it parse is the fabrication this pipeline exists
    to avoid.
    """
    if not value:
        return None, False
    text = value.strip()

    try:
        parsed = datetime.date.fromisoformat(text[:10])
        return datetime.datetime.combine(parsed, datetime.time.min, tzinfo=datetime.UTC), False
    except (ValueError, TypeError):
        pass

    match = _DATE_RE.search(text)
    if not match:
        return None, False
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None, False
    day = int(match.group("day"))
    year_text = match.group("year")
    inferred = year_text is None
    if year_text is not None:
        year = int(year_text)
    elif sent_at is not None:
        # The year the message was sent. A deadline stated as "Jan. 15" in a
        # December email means the following January, so roll forward when the
        # date would otherwise land in the past.
        year = sent_at.year
        try:
            candidate = datetime.date(year, month, day)
        except ValueError:
            return None, False
        if candidate < sent_at.date():
            year += 1
    else:
        return None, False

    try:
        resolved = datetime.date(year, month, day)
    except ValueError:
        return None, False
    return datetime.datetime.combine(resolved, datetime.time.min, tzinfo=datetime.UTC), inferred


def _spec_text(row: dict[str, Any]) -> str:
    return (
        f"From: {row.get('from_email') or ''}\n"
        f"Subject: {row.get('subject') or ''}\n"
        f"Date: {row.get('sent_at') or ''}\n\n"
        f"{(row.get('body_text') or '')[:20000]}"
    )


async def handle_classify_mail(task_id: int, payload: dict[str, Any]) -> None:
    from openai.lib._pydantic import to_strict_json_schema

    from core.batch import BatchSpec

    backfill = bool(payload.get("backfill"))
    model = BACKFILL_MODEL if backfill else ONGOING_MODEL
    # Clamped rather than trusted: an enqueuer asking for the whole mailbox in
    # one task would build a spec list far larger than a wave can carry, and
    # the failure would arrive as memory pressure on a worker rather than as a
    # rejected parameter.
    cap = min(int(payload.get("cap") or CLASSIFY_PER_CYCLE), MAX_CLASSIFY_PER_CYCLE)
    rows = db.query(
        """
        SELECT m.id, m.from_email, m.subject, m.sent_at, m.body_text
        FROM email_messages m
        WHERE NOT EXISTS (SELECT 1 FROM email_events e WHERE e.message_id = m.id)
        -- Likely job mail first. The prefilter gates nothing, so this only
        -- changes the ORDER in which the whole mailbox is worked through -
        -- which matters because the useful results arrive sooner.
        ORDER BY m.prefilter_hit DESC NULLS LAST, m.id DESC
        LIMIT %(cap)s
        """,
        {"cap": cap},
    )
    if not rows:
        _set_progress(task_id, 0, 0, "nothing to classify")
        return

    by_id = {r["id"]: r["sent_at"] for r in rows}
    schema = to_strict_json_schema(MailClassification)
    specs = [
        BatchSpec(str(r["id"]), _INSTRUCTIONS, _spec_text(r), "MailClassification", schema)
        for r in rows
    ]
    _set_progress(task_id, 0, len(specs), f"mail classification submitted ({model}, half price)")
    hook = _batch_event_hook(task_id, "mail_classify", model)
    results = await submit_or_collect(
        task_id, specs, model, effort_for(model), CLASSIFY_MAX_TOKENS, hook
    )

    done = 0
    skipped = 0
    for key, res in results.items():
        if res.error or not res.text:
            continue
        try:
            parsed = MailClassification.model_validate_json(res.text)
        except Exception:
            logger.warning(f"mail classify: unparsable output for message {key}")
            continue
        # Everything that can raise happens BEFORE anything is written, so a
        # half-parsed result cannot produce a half-written row that later looks
        # like a completed classification.
        deadline, year_inferred = parse_deadline(
            parsed.deadline, sent_at=by_id.get(int(key)) if key.isdigit() else None
        )
        try:
            db.execute(
                """
                INSERT INTO email_events (
                    message_id, kind, confidence, deadline_at, deadline_inferred, detail, model
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    int(key),
                    parsed.kind,
                    parsed.confidence,
                    deadline,
                    # Inferred if the model said so, OR if we resolved a
                    # missing year ourselves. Either way it is not a date the
                    # email stated outright.
                    deadline is not None and (year_inferred or not parsed.deadline_is_explicit),
                    db.jsonb({"company": parsed.company, "role_title": parsed.role_title}),
                    model,
                ),
            )
        except Exception:
            # One malformed field must not discard the whole batch's results.
            # The batch is already paid for; losing 4,999 good classifications
            # to one bad row is the expensive way to be strict, and the row is
            # picked up again next sweep because it has no event.
            logger.warning(f"mail classify: could not record message {key}", exc_info=True)
            skipped += 1
            continue
        done += 1
        if done % 200 == 0:
            _set_progress(task_id, done, len(specs), "mail classified")
    _set_progress(task_id, done, len(specs), "mail classified")
    if skipped:
        logger.warning(f"mail classify: {skipped} row(s) skipped on write")
