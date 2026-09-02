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

import dataclasses
import datetime
import logging
import os
import re
import zoneinfo
from typing import Any, Literal

from pydantic import BaseModel

from api import db
from api.tasks.runtime import _set_progress, run_batched
from core.providers.spec import StructuredOutput
from core.routing import Evidence, TaskShape, resolve

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
_EFFORT_PREFERENCE = ("none", "minimal", "low")

# Every model in the intersection above, so a model the registry has not been
# taught still gets a value both generations accept rather than failing the
# whole batch. Deliberately not the cheapest: guessing cheap at an unknown
# model is how the 400 happened.
FALLBACK_EFFORT = "low"


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
        effort_preference=_EFFORT_PREFERENCE,
        candidates=(model,),
    )


BACKFILL_TASK = _classify_task(
    BACKFILL_MODEL, "mail_classify_backfill", "Mail classification (backfill)"
)
ONGOING_TASK = _classify_task(ONGOING_MODEL, "mail_classify", "Mail classification (ongoing)")


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

"offer" is an offer of EMPLOYMENT - a job, internship or paid position the
candidate can accept and be paid for. Nothing else is an offer, however it is
worded:

- An "offer of admission" to a university, degree or programme is NOT an offer,
  even when it names money. Tuition credits, enrolment deposits and
  scholarships are not pay.
- Acceptance into a course, a hackathon RSVP or team allocation, a club or
  conference allotment, and "you're in" or "confirm your spot" mail are
  "not_job_related".
- Discussing a job already started, or congratulating someone on one, is not
  an offer.

If the email is about paid work but does not extend an offer, use the kind
that fits what it does say.

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

# The ordinal suffix is optional and must be consumed: "June 15th" is
# extremely common in this corpus and without it the day fails to terminate,
# so the whole date was dropped.
#
# This is a search rather than a match, which is why weekday prefixes
# ("Thursday, February 1, 2024") and trailing text ("at 4:00PM (EST)") never
# broke it. What they DID break is silently worse - see _TIME_RE.
_DATE_RE = re.compile(
    r"\b(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,)?(?:\s+(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)

# The time was thrown away on every row. Before this, every one of the 1,356
# stored deadlines was exactly 00:00 UTC, including the ones that came from
# "Tue Dec 10, 2024 at 4:00PM (EST)" - a real instant of 21:00 UTC recorded 21
# hours early, and looking entirely healthy. A wrong timestamp that parses is
# worse than one that does not, so the time is captured now.
_TIME_RE = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)\b",
    re.IGNORECASE,
)

# Labels mapped to IANA ZONES rather than fixed offsets, deliberately. "Pacific
# Time" in June is PDT and in December is PST; a fixed -08:00 would be an hour
# out for half the year. zoneinfo resolves it from the date itself.
#
# "EST" written in July is treated the same way - as Eastern, not as a literal
# -05:00 - because people write the standard-time abbreviation year round and
# mean the zone. That is a judgement call and it is the one that is right more
# often on this corpus.
_ZONES = {
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
    "eastern": "America/New_York",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "central": "America/Chicago",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "mountain": "America/Denver",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "utc": "UTC",
    "gmt": "UTC",
}
_ZONE_RE = re.compile(
    r"\b(" + "|".join(sorted(_ZONES, key=len, reverse=True)) + r")\b(?:\s+time)?",
    re.IGNORECASE,
)

# Only these roll a bare "Jan. 15" forward into next year. A date without a
# year in a rejection or an acknowledgement is almost always the date you
# APPLIED, and rolling it forward invents a future deadline out of a past
# event. The list is an allowlist rather than a blocklist so that a kind added
# later does not silently inherit the roll.
_FUTURE_FACING_KINDS = frozenset(
    {
        "assessment_invite",
        "interview_invite",
        "interview_scheduled",
        "info_request",
        "offer",
        "recruiter_outreach",
    }
)

# An interview that has been scheduled has a TIME, not a deadline. Everything
# else states a date you must act by. Splitting on kind rather than on "did we
# find a time" is what distinguishes an appointment from "submit by March 1,
# 5:00 PM Pacific", which is a deadline that happens to carry a clock time.
_APPOINTMENT_KINDS = frozenset({"interview_scheduled"})


# Mail the candidate SENT is not an inbound event about the candidate. The
# instruction asks the model what "the SENDER is communicating", and when the
# sender is the candidate every kind comes out backwards - his own reply to a
# professor saying he had started work and was enjoying it was classified as an
# offer. 942 messages are self-sent and 137 of them carried inbound events,
# inflating the interviewing and offer counts directly.
#
# Direction is knowable from the header, so it is read rather than inferred.
# Handing it to a model turns a fact into a probability.
#
# THE MESSAGES ARE KEPT, only their classification as inbound events is
# skipped. Sent mail is the best record that exists of WHEN the candidate
# applied - better than applications.applied_at, which currently infers it from
# the first inbound reply - and that is a different signal with a different
# shape, to be built deliberately rather than fall out of a misread event kind.
_SELF_SENT = "(u.email <> '' AND position(lower(u.email) in lower(COALESCE(m.from_email, ''))) > 0)"


@dataclasses.dataclass(frozen=True)
class ParsedWhen:
    """A moment an email referred to, and how much of it was actually stated.

    `at` is always aware UTC. `is_instant` says whether it is a real moment or
    only a date: a stated time with no stated zone is NOT resolvable, because
    the fleet runs containers on America/New_York while Postgres is UTC, so
    picking either would be a coin flip dressed as a fact. Such a value keeps
    the date and drops the clock rather than assuming an offset.
    """

    at: datetime.datetime
    is_instant: bool
    year_inferred: bool


def _parse_time(text: str) -> tuple[int, int] | None:
    match = _TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    if not (1 <= hour <= 12) or minute > 59:
        return None
    meridiem = match.group("meridiem").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    return hour, minute


def _parse_zone(text: str) -> zoneinfo.ZoneInfo | None:
    match = _ZONE_RE.search(text)
    if not match:
        return None
    try:
        return zoneinfo.ZoneInfo(_ZONES[match.group(1).lower()])
    except Exception:
        return None


def parse_when(
    value: str | None,
    *,
    sent_at: datetime.datetime | None = None,
    kind: str | None = None,
) -> ParsedWhen | None:
    """The moment an email named, or nothing. Never a guess.

    Four outcomes, deliberately distinct:

    A full ISO timestamp or prose date resolves exactly.

    A date with no year resolves against the message it came from, and only
    for a forward-looking kind - "Jan. 15" in a rejection is the day you
    applied, not next January. Marked inferred either way.

    A date with a time AND a zone resolves to a true instant. A date with a
    time but NO zone keeps the date and drops the time, because a naive local
    time stored as UTC is a silently wrong answer rather than a missing one.

    Anything with no resolvable date at all ("Tuesday at 2:00 PM", "soon")
    becomes nothing, and the caller keeps the raw string.
    """
    if not value:
        return None
    text = value.strip()

    try:
        iso = datetime.datetime.fromisoformat(text)
    except ValueError:
        iso = None
    if iso is not None:
        if iso.tzinfo is not None:
            return ParsedWhen(iso.astimezone(datetime.UTC), True, False)
        # A naive ISO timestamp carries a clock with no zone, which is the same
        # unresolvable case as "9:00 AM" with no zone below. Keeping the date
        # and dropping the time is the consistent answer; reading it as UTC
        # would be inventing an offset in the one branch that looks precise.
        return ParsedWhen(
            datetime.datetime.combine(iso.date(), datetime.time.min, tzinfo=datetime.UTC),
            False,
            False,
        )

    match = _DATE_RE.search(text)
    if not match:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    day = int(match.group("day"))
    year_text = match.group("year")
    year_inferred = year_text is None
    if year_text is not None:
        year = int(year_text)
    elif sent_at is not None:
        year = sent_at.year
        try:
            candidate = datetime.date(year, month, day)
        except ValueError:
            return None
        if candidate < sent_at.date() and (kind in _FUTURE_FACING_KINDS):
            year += 1
    else:
        return None

    try:
        resolved = datetime.date(year, month, day)
    except ValueError:
        return None

    clock = _parse_time(text)
    zone = _parse_zone(text) if clock else None
    if clock and zone is not None:
        local = datetime.datetime(resolved.year, resolved.month, resolved.day, *clock, tzinfo=zone)
        return ParsedWhen(local.astimezone(datetime.UTC), True, year_inferred)
    return ParsedWhen(
        datetime.datetime.combine(resolved, datetime.time.min, tzinfo=datetime.UTC),
        False,
        year_inferred,
    )


def is_appointment(kind: str | None) -> bool:
    return kind in _APPOINTMENT_KINDS


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
    shape = BACKFILL_TASK if backfill else ONGOING_TASK
    model = resolve(shape).model
    # Clamped rather than trusted: an enqueuer asking for the whole mailbox in
    # one task would build a spec list far larger than a wave can carry, and
    # the failure would arrive as memory pressure on a worker rather than as a
    # rejected parameter.
    cap = min(int(payload.get("cap") or CLASSIFY_PER_CYCLE), MAX_CLASSIFY_PER_CYCLE)
    # Re-classifying an EXPLICIT set of messages, for repairing events that were
    # written wrong rather than for finding ones that are missing. The set is
    # computed by the caller and recorded in the payload, so the task row says
    # exactly what was re-classified and why it was expected to be that many.
    #
    # An explicit id list rather than a "reclassify" mode carrying its own WHERE
    # clause: a predicate the handler evaluates can match the whole mailbox if
    # someone later widens it, and the cost of that mistake is a re-paid
    # backfill discovered on the bill. A list cannot grow on its own.
    #
    # Events are append-only and the latest per message wins
    # (mail_pipeline's DISTINCT ON ... ORDER BY id DESC), so a corrected event
    # supersedes the old one. Nothing is deleted and nothing is migrated.
    requested = payload.get("message_ids")
    if requested:
        ids = sorted({int(i) for i in requested})
        if len(ids) > cap:
            # Refused rather than truncated. Silently doing part of a repair
            # leaves the rest wrong with nothing recording which half ran.
            raise ValueError(
                f"message_ids has {len(ids)} entries, over the cap of {cap}; "
                "split it into several tasks"
            )
        rows = db.query(
            f"""
            SELECT m.id, m.from_email, m.subject, m.sent_at, m.body_text,
                   {_SELF_SENT} AS self_sent
            FROM email_messages m
            JOIN users u ON u.id = m.user_id
            WHERE m.id = ANY(%(ids)s) ORDER BY m.id
            """,
            {"ids": ids},
        )
        logger.info(f"Task {task_id}: re-classifying {len(rows)} of {len(ids)} requested messages")
    else:
        rows = db.query(
            f"""
            SELECT m.id, m.from_email, m.subject, m.sent_at, m.body_text,
                   FALSE AS self_sent
            FROM email_messages m
            JOIN users u ON u.id = m.user_id
            WHERE NOT EXISTS (SELECT 1 FROM email_events e WHERE e.message_id = m.id)
              AND NOT {_SELF_SENT}
              -- Not already paid for. "No events yet" is true of a message the
              -- moment it is submitted and stays true for the hours it waits in
              -- the provider's queue, so a later sweep selected the SAME
              -- messages and submitted them again: three tasks an hour apart
              -- each carrying an identical 1,156 requests, all in flight at
              -- once. The cost is not the queue depth, it is paying repeatedly
              -- for one answer.
              --
              -- Reading the in-flight ids from the parked tasks rather than
              -- from a column, because the payload is where submit_or_collect
              -- already records them and a second copy would be a second thing
              -- to keep true.
              AND NOT EXISTS (
                  SELECT 1 FROM tasks t
                  WHERE t.kind = 'classify_mail'
                    AND t.status IN ('pending', 'running', 'waiting', 'awaiting_batch')
                    AND t.payload -> 'claimed_message_ids' @> to_jsonb(m.id)
              )
            -- Likely job mail first. The prefilter gates nothing, so this only
            -- changes the ORDER in which the whole mailbox is worked through -
            -- which matters because the useful results arrive sooner.
            ORDER BY m.prefilter_hit DESC NULLS LAST, m.id DESC
            LIMIT %(cap)s
            """,
            {"cap": cap},
        )
    # Self-sent mail only reaches here through an explicit message_ids repair,
    # because the sweep excludes it. Correcting one needs no model: the header
    # already settles direction, so the event is written directly, costs
    # nothing, and supersedes the wrong one by the same latest-wins rule. Left
    # to the model these come back as offers and interviews - that is what the
    # 137 already-written events are.
    corrected = [r for r in rows if r["self_sent"]]
    rows = [r for r in rows if not r["self_sent"]]
    for row in corrected:
        db.execute(
            """
            INSERT INTO email_events (message_id, kind, confidence, detail, model)
            VALUES (%s, 'not_job_related', 'high', %s, NULL)
            """,
            (row["id"], db.jsonb({"reason": "self_sent"})),
        )
    if corrected:
        logger.info(f"Task {task_id}: corrected {len(corrected)} self-sent message(s)")

    if not rows:
        _set_progress(task_id, len(corrected), len(corrected), "nothing to classify")
        return

    # Record what this task claimed BEFORE submitting, so a sweep an hour from
    # now can see it. "No events yet" stays true for the whole time a message
    # sits in the provider's queue, so without this the next sweep selects the
    # same messages and pays for them again - which is exactly what happened:
    # three tasks an hour apart each carrying an identical 1,156 requests.
    #
    # Merged rather than replaced, because submit_or_collect writes batch_ids
    # into this same payload and a requeued attempt reattaches through it.
    db.execute(
        "UPDATE tasks SET payload = COALESCE(payload, '{}'::jsonb) || %s WHERE id = %s",
        (db.jsonb({"claimed_message_ids": [r["id"] for r in rows]}), task_id),
    )

    by_id = {r["id"]: r["sent_at"] for r in rows}
    schema = to_strict_json_schema(MailClassification)
    specs = [
        BatchSpec(str(r["id"]), _INSTRUCTIONS, _spec_text(r), "MailClassification", schema)
        for r in rows
    ]
    _set_progress(task_id, 0, len(specs), f"mail classification submitted ({model}, half price)")
    results, _ = await run_batched(task_id, shape, specs)

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
        when = parse_when(
            parsed.deadline,
            sent_at=by_id.get(int(key)) if key.isdigit() else None,
            kind=parsed.kind,
        )
        # An interview that has been scheduled is an appointment, not something
        # to act by. Until now every one of these went into deadline_at with
        # its time stripped, while occurred_at - which mail_pipeline already
        # reads - was never written at all.
        occurred_at: datetime.datetime | None = None
        deadline_at: datetime.datetime | None = None
        year_inferred = False
        # Mail that is not about a job has no job deadline, whatever date it
        # states. This is the model's own answer applied to its own other
        # answer, not a second judgement: it already said the message is not
        # job related, so the date it found is a marketing expiry, a newsletter
        # RSVP or a tuition date.
        #
        # It is the largest source of deadlines in the corpus by a wide margin
        # - 1,409 of 2,128, two thirds of every deadline recorded - and each
        # one becomes an action item and feeds "quiet for 60+ days".
        if when is not None and parsed.kind != "not_job_related":
            year_inferred = when.year_inferred
            if is_appointment(parsed.kind):
                occurred_at = when.at
            else:
                deadline_at = when.at
        detail: dict[str, Any] = {"company": parsed.company, "role_title": parsed.role_title}
        if parsed.deadline:
            # The raw string is kept whether or not it parsed. Keeping only the
            # successes means the failures cannot be studied - which is exactly
            # the position this parser was rewritten from, with no record of
            # what it had been unable to read.
            detail["when_raw"] = parsed.deadline
            detail["when_dropped_as_not_job_related"] = (
                parsed.kind == "not_job_related" and when is not None
            )
            detail["when_precision"] = (
                None if when is None else ("instant" if when.is_instant else "date")
            )
        try:
            db.execute(
                """
                INSERT INTO email_events (
                    message_id, kind, confidence, occurred_at, deadline_at,
                    deadline_inferred, detail, model
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    int(key),
                    parsed.kind,
                    parsed.confidence,
                    occurred_at,
                    deadline_at,
                    # Inferred if the model said so, OR if we resolved a
                    # missing year ourselves. Either way it is not a date the
                    # email stated outright.
                    deadline_at is not None and (year_inferred or not parsed.deadline_is_explicit),
                    db.jsonb(detail),
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
