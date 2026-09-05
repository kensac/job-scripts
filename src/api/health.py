from __future__ import annotations

import logging
from typing import Any

from api import db, telemetry

logger = logging.getLogger("jobtracker_health")

# Minimum sample sizes: below these a "rate" is noise, and alerting on noise
# trains you to ignore alerts. The rate floor is deliberately high because the
# samples are not independent, see MAX_PER_COMPANY.
MIN_SAMPLES = 50
MIN_CONTENT_SAMPLES = 10

# One employer bulk-posting a batch of no-sponsorship roles is a single
# editorial decision, not N independent observations. Capping each company's
# contribution per window keeps a seasonal drop (Qorvo posted 36 internships in
# one day, 31 of them clearance-restricted) from moving a whole source's rate.
MAX_PER_COMPANY = 5

# A job whose first-ever check lands long after we catalogued it came from a
# backlog sweep, not the live feed. Those are systematically staler, and more
# often closed or restricted, than freshly-ingested postings, so mixing them
# in makes a coverage change look like breakage. This is the same confound
# #110 removed for re-checks, one level down: fetch_missing_content gives old
# jobs their FIRST check, so restricting to first-ever checks does not exclude
# it on its own.
FRESH_CHECK_WINDOW = "3 days"

# How long a condition must go undetected before it is considered over. A
# detector stops firing when it stops being EVALUABLE (sample count dipped
# under the floor, the 24h window rolled past the event) at least as often as
# when the condition ends; resolving on the first miss makes those alerts
# reopen an hour later and re-notify. Re-firing inside the grace period just
# refreshes the open row, so no second email goes out.
RESOLVE_GRACE = "3 hours"


# What an alert's `subject` actually holds, per detector.
#
# It is not one kind of thing: two detectors put a source in it, one a host,
# one a provider and user id, one a task kind. The dashboard linked all of them
# to /job-scripts/sources?src=<subject>, which is right for two of five - the
# other three sent an operator to a page that selects nothing, and an empty
# sources page reads as a source that has disappeared rather than as a link
# that was never right.
#
# Declared here rather than guessed from the value client-side, which would be
# the frontend encoding semantics it cannot see.
#
# Kept as a map from alert kind rather than a column on health_alerts, because
# it is a property of the DETECTOR and never varies between two alerts of the
# same kind. A column would store the same answer on every row, go stale if a
# detector changed what it puts in subject, and need a backfill to say anything
# about the alerts already open.
SUBJECT_SOURCE = "source"
SUBJECT_HOST = "host"
SUBJECT_PROVIDER_USER = "provider_user"
SUBJECT_TASK = "task"
# The spend ledger's grouping key, not a task kind: the batch purpose is
# "mail_classify" where the task kind is "classify_mail". Linking one to the
# other lands on nothing.
SUBJECT_PURPOSE = "purpose"
# A fleet worker's name (tasks.worker), not a URL host.
SUBJECT_WORKER = "worker"
# An applicant-tracking system family (greenhouse, lever, ...), not one host.
SUBJECT_ATS = "ats"
# A task KIND (extract_comp), not a task id.
SUBJECT_TASK_KIND = "task_kind"
# One of the detector sections in this module, when it is the thing broken.
SUBJECT_DETECTOR = "detector"

_SUBJECT_KINDS = {
    "ats_text_collapse": SUBJECT_SOURCE,
    "extraction_failing": SUBJECT_HOST,
    "oauth_token_invalid": SUBJECT_PROVIDER_USER,
    "batch_parked_too_long": SUBJECT_TASK,
    "batch_failed_whole": SUBJECT_PURPOSE,
    "ingest_failing": SUBJECT_SOURCE,
    "ingest_host_failing": SUBJECT_HOST,
    "source_feed_empty": SUBJECT_SOURCE,
    "source_pattern_excludes_all": SUBJECT_SOURCE,
    "worker_fetches_failing": SUBJECT_WORKER,
    "resolver_bypassed": SUBJECT_ATS,
    "queue_stalled": SUBJECT_WORKER,
    "ingest_backlog": SUBJECT_TASK,
    "fleet_mixed_release": SUBJECT_WORKER,
    "source_pattern_admits_all": SUBJECT_SOURCE,
    "sweep_did_nothing": SUBJECT_TASK_KIND,
    "task_kind_failing": SUBJECT_TASK_KIND,
    "task_requeued_forever": SUBJECT_TASK,
    "alerts_unnotified": SUBJECT_DETECTOR,
    "detector_failed": SUBJECT_DETECTOR,
}


def subject_kind_for(alert_kind: str) -> str | None:
    """What this kind of alert puts in its subject, or None when unknown.

    None rather than a default: a wrong link is worse than no link, which is
    the whole reason this exists. A detector added without an entry here gets
    plain text, which is honest, instead of inheriting whatever the last one
    used.

    The rate-spike kinds are generated per check_type, so they are matched by
    shape rather than listed - listing them would mean a new check_type
    silently losing its link.
    """
    if alert_kind.endswith("_rate_spike"):
        return SUBJECT_SOURCE
    return _SUBJECT_KINDS.get(alert_kind)


def _pct(part: int, whole: int) -> float:
    return (part / whole) if whole else 0.0


def detect() -> list[dict[str, Any]]:
    """Compares the last 24h against the preceding week, per source, looking for
    the shapes that mean 'something upstream changed' rather than 'the job
    market moved'. Everything here is deliberately relative to each source's
    own baseline. Absolute thresholds would fire constantly on sources that
    are legitimately mostly-closed or legitimately short."""
    found: list[dict[str, Any]] = []

    # 1. The ATS text path silently breaking. When a resolver stops returning
    #    usable text we fall back to chromium, so the share collapses long
    #    before anything is visibly wrong. This is the earliest warning we get.
    for r in db.query(
        """
        SELECT j.source,
               COUNT(*) FILTER (WHERE q.created_at > now() - interval '24 hours') AS recent_total,
               COUNT(*) FILTER (WHERE q.created_at > now() - interval '24 hours'
                                AND q.reason = 'ats text') AS recent_ats,
               COUNT(*) FILTER (WHERE q.created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours') AS base_total,
               COUNT(*) FILTER (WHERE q.created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours' AND q.reason = 'ats text') AS base_ats
        FROM ai_queries q JOIN jobs j ON j.url = q.url
        WHERE q.check_type = 'content'
          -- Only rows that record where the text CAME from. Other writers
          -- (pittcsc's 'content cached') log a content row with no origin,
          -- and counting those in the denominator silently buries the ATS
          -- share far below the `base >= 0.30` floor, which is why this
          -- detector had never once fired.
          AND q.reason IN ('ats text', 'scraped', 'static')
          AND q.created_at > now() - interval '8 days'
          -- Backlog sweeps and live ingest are different populations with
          -- different ATS-text shares, so comparing a backfill-heavy baseline
          -- against a live-traffic window invents a collapse (it does exactly
          -- that today, in the direction the old gate did NOT anticipate:
          -- backfilled jobs resolve to ATS text MORE often, not less).
          -- Excluding them beats suppressing the detector wholesale.
          AND q.created_at - j.created_at < %(fresh_window)s::interval
        GROUP BY j.source
        """,
        {"fresh_window": FRESH_CHECK_WINDOW},
    ):
        if r["recent_total"] < MIN_CONTENT_SAMPLES or r["base_total"] < MIN_CONTENT_SAMPLES:
            continue
        recent = _pct(r["recent_ats"], r["recent_total"])
        base = _pct(r["base_ats"], r["base_total"])
        # Only meaningful if the source actually relied on ATS text before.
        if base >= 0.30 and recent < base * 0.5:
            found.append(
                {
                    "kind": "ats_text_collapse",
                    "subject": r["source"],
                    "severity": "critical",
                    "message": (
                        f"ATS text share for {r['source']} fell from {base:.0%} to {recent:.0%}. "
                        "The resolver is probably broken and we're paying to scrape instead."
                    ),
                    "detail": dict(r),
                }
            )

    # 2. Verdict-rate shifts on FIRST-EVER checks only. Re-checks flipping to
    #    closed is the system working. Postings expire, and a sweep that newly
    #    covers a backlog of old jobs will legitimately reject a lot of them at
    #    once. Mixing the two makes coverage changes look like breakage (it did:
    #    the first live alert was a reverify backlog on a board whose postings
    #    expire in ~2 days). What genuinely indicates something upstream broke
    #    is FRESHLY-SEEN jobs being classified closed at an unusual rate.
    #
    #    Two confounds survive the first-ever restriction and are handled in the
    #    CTE: backlog sweeps hand OLD jobs their first check (FRESH_CHECK_WINDOW),
    #    and one employer's bulk drop is one decision rather than N independent
    #    samples (MAX_PER_COMPANY). Both are applied per window so the recent and
    #    baseline rates stay comparable.
    for r in db.query(
        """
        WITH firsts AS (
            SELECT j.source, q.check_type, q.status,
                   q.created_at > now() - interval '24 hours' AS is_recent,
                   ROW_NUMBER() OVER (
                       PARTITION BY j.source, q.check_type, COALESCE(q.company, ''),
                                    q.created_at > now() - interval '24 hours'
                       ORDER BY q.id
                   ) AS company_rank
            FROM ai_queries q JOIN jobs j ON j.url = q.url
            WHERE q.check_type IN ('closed', 'clearance')
              AND q.status IN ('passed', 'rejected')
              AND q.created_at > now() - interval '8 days'
              AND q.created_at - j.created_at
                  < %(fresh_window)s::interval
              AND NOT EXISTS (
                SELECT 1 FROM ai_queries p
                WHERE p.url = q.url AND p.check_type = q.check_type
                  AND p.id < q.id AND p.status IN ('passed', 'rejected'))
        )
        SELECT source, check_type,
               COUNT(*) FILTER (WHERE is_recent) AS recent_total,
               COUNT(*) FILTER (WHERE is_recent AND status = 'rejected') AS recent_rejected,
               COUNT(*) FILTER (WHERE NOT is_recent) AS base_total,
               COUNT(*) FILTER (WHERE NOT is_recent AND status = 'rejected') AS base_rejected
        FROM firsts WHERE company_rank <= %(cap)s
        GROUP BY source, check_type
        """,
        {"fresh_window": FRESH_CHECK_WINDOW, "cap": MAX_PER_COMPANY},
    ):
        if r["recent_total"] < MIN_SAMPLES or r["base_total"] < MIN_SAMPLES:
            continue
        recent = _pct(r["recent_rejected"], r["recent_total"])
        base = _pct(r["base_rejected"], r["base_total"])
        if recent - base >= 0.25 and recent >= 0.35:
            found.append(
                {
                    "kind": f"{r['check_type']}_rate_spike",
                    "subject": r["source"],
                    "severity": "critical",
                    "message": (
                        f"{r['check_type']} rejection rate for newly-seen {r['source']} jobs "
                        f"jumped from {base:.0%} to {recent:.0%} over {r['recent_total']} "
                        f"first-time checks (max {MAX_PER_COMPANY} per company). Jobs are "
                        "being written off on arrival, so suspect the input text before "
                        "believing the verdicts."
                    ),
                    "detail": dict(r),
                }
            )

    # 3. Extraction failures concentrated on one host: the bot-wall signature.
    #    Relative to the host's own prior week, not an absolute rate. A site
    #    that has always failed 60% is a known cost of doing business, and an
    #    alert that fires forever on it is one the reader learns to skip. What
    #    matters is a host that STARTED failing.
    for r in db.query(
        """
        SELECT substring(url from '//([^/]+)') AS host,
               COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours') AS recent_total,
               COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours'
                                AND status = 'failed') AS recent_failed,
               COUNT(*) FILTER (WHERE created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours') AS base_total,
               COUNT(*) FILTER (WHERE created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours' AND status = 'failed') AS base_failed
        FROM ai_queries
        WHERE check_type IN ('extraction', 'content')
          AND created_at > now() - interval '8 days'
        GROUP BY 1
        """
    ):
        if r["recent_total"] < 20 or r["base_total"] < 20:
            continue
        recent = _pct(r["recent_failed"], r["recent_total"])
        base = _pct(r["base_failed"], r["base_total"])
        if recent - base >= 0.25 and recent >= 0.5:
            found.append(
                {
                    "kind": "extraction_failing",
                    "subject": r["host"],
                    "severity": "warning",
                    "message": (
                        f"{r['recent_failed']} of {r['recent_total']} fetches from {r['host']} "
                        f"failed in 24h ({recent:.0%}, up from {base:.0%} over the prior week). "
                        "Blocked, or the page shape changed."
                    ),
                    "detail": dict(r),
                }
            )

    # A stored mailbox credential the provider has rejected. Nothing else in
    # the system surfaces this: mail ingest simply stops finding anything,
    # which is indistinguishable from a quiet week. The OAuth client is in
    # Testing mode with a restricted scope, so Google expires its refresh
    # tokens after seven days and this is expected to fire on that cadence -
    # it is the reconnect prompt, not a symptom of a bug.
    for r in db.query(
        """
        SELECT t.user_id, t.provider, t.account_email, t.invalid_reason,
               u.email AS user_email,
               date_trunc('second', now() - t.invalid_at)::text AS dead_for
        FROM user_oauth_tokens t JOIN users u ON u.id = t.user_id
        WHERE t.invalid_at IS NOT NULL
        """
    ):
        mailbox = r["account_email"] or r["user_email"] or f"user {r['user_id']}"
        found.append(
            {
                "kind": "oauth_token_invalid",
                # Per (provider, user) rather than per mailbox: the address is
                # nullable, and the alert must still be unique without it.
                "subject": f"{r['provider']}:{r['user_id']}",
                "severity": "warning",
                "message": (
                    f"{r['provider']} access for {mailbox} was rejected "
                    f"{r['dead_for']} ago ({r['invalid_reason']}). Mail ingest is "
                    "stopped until it is reconnected in tracker settings."
                ),
                "detail": dict(r),
            }
        )

    # A task parked on a provider batch, past the provider's own guarantee.
    #
    # This matters more now that every batched sweep refuses to start while one
    # of its own is in flight. That guard stops double payment and converts a
    # stuck task into a SILENT stall: the sweep simply never runs again, and
    # nothing else says so. comp, requirements and mail classification have had
    # that property for a while and verify_new now has it too.
    #
    # The threshold is the provider's completion window rather than a picked
    # number of hours. Inside it, waiting is what a batch is supposed to do;
    # past it, the provider has broken its own promise AND poll_batches has
    # failed to give up on it, which is two things wrong at once. It moves
    # automatically if BATCH_COMPLETION_WINDOW ever changes.
    from core.batch import completion_window_seconds

    window = completion_window_seconds()
    for r in db.query(
        """
        SELECT kind, COUNT(*) AS parked,
               MAX(EXTRACT(epoch FROM now() - COALESCE(started_at, created_at))) AS oldest_secs
        FROM tasks
        WHERE status = 'awaiting_batch'
          AND COALESCE(started_at, created_at) < now() - make_interval(secs => %(window)s)
        GROUP BY kind
        """,
        {"window": window},
    ):
        hours = (r["oldest_secs"] or 0) / 3600
        found.append(
            {
                "kind": "batch_parked_too_long",
                "subject": r["kind"],
                "severity": "critical",
                "message": (
                    f"{r['parked']} {r['kind']} task(s) have been waiting on a provider "
                    f"batch for up to {hours:.0f}h, past the {window / 3600:.0f}h completion "
                    "window. The sweep does not start another while one is in flight, so "
                    "this task is not running at all."
                ),
                "detail": {
                    "kind": r["kind"],
                    "parked": r["parked"],
                    "oldest_hours": round(hours, 1),
                },
            }
        )

    # A batch is submitted whole and fails whole, so failed_count = requests is
    # a DIFFERENT event from some requests failing: it means the submission was
    # rejected on grounds that applied to every request in it (an unsupported
    # reasoning_effort, a model that will not take the schema), not that some
    # inputs were bad. That distinction is the threshold - there is no picked
    # failure rate here, because the two cases have different causes and only
    # this one is certainly a defect.
    #
    # Nothing else reports it. The task finishes 'done' with no error, because
    # collection succeeded at collecting nothing: on 2026-09-02 a 499-request
    # mail_classify batch failed every request and its task closed clean, which
    # is the same summary-line-is-not-the-measurement shape as a clean exit
    # reading as success. Zero tokens and zero cost are then CORRECT - nothing
    # ran - so the spend ledger cannot see it either.
    #
    # Bounded to one completion window for the same reason the parked detector
    # uses it: inside that window the work can still be resubmitted, so the
    # alert is actionable. Older ones are history and would alarm forever.
    #
    # And bounded by the purpose's own recovery: a whole failure that a later
    # batch for the same purpose survived is fixed, whatever fixed it, and an
    # alert that outlives its fix by the rest of the window trains the reader
    # to wait it out. On 2026-09-04 requirements failed whole on gpt-5-nano
    # from 07:00 to 16:00 and succeeded on the sanctioned model from 17:00;
    # the alert stayed open past 19:00 on the morning's batches alone.
    #
    # The message carries the provider's reason where one was stored
    # (ai_batch_errors, the most frequent text): "rejected" alone left the
    # 21,525-request failure unanswerable.
    for r in db.query(
        """
        WITH failed AS (
            SELECT provider_batch_id, purpose, model, requests, submitted_at
            FROM ai_batches b
            WHERE requests > 0 AND failed_count = requests
              AND submitted_at > now() - make_interval(secs => %(window)s)
              AND NOT EXISTS (
                SELECT 1 FROM ai_batches later
                WHERE later.purpose = b.purpose AND later.submitted_at > b.submitted_at
                  AND later.requests > 0 AND later.failed_count < later.requests
                  AND later.status IN ('completed', 'failed', 'expired', 'cancelled'))
        )
        SELECT purpose, model, COUNT(*) AS batches, SUM(requests) AS requests,
               MAX(EXTRACT(epoch FROM now() - submitted_at)) AS oldest_secs,
               (SELECT e.error FROM ai_batch_errors e
                WHERE e.provider_batch_id IN (SELECT provider_batch_id FROM failed f2
                                              WHERE f2.purpose = f.purpose)
                GROUP BY e.error ORDER BY COUNT(*) DESC LIMIT 1) AS reason
        FROM failed f
        GROUP BY purpose, model
        """,
        {"window": window},
    ):
        reason = f" The provider said: {r['reason']}" if r["reason"] else ""
        found.append(
            {
                "kind": "batch_failed_whole",
                "subject": r["purpose"],
                "severity": "critical",
                "message": (
                    f"{r['batches']} {r['purpose']} batch(es) on {r['model']} came back with "
                    f"every one of their {r['requests']} requests failed. A batch fails whole, "
                    "so this is the submission being rejected rather than bad inputs. The task "
                    "finishes 'done' with no error and no cost, so nothing else reports it."
                    f"{reason}"
                ),
                "detail": {
                    "purpose": r["purpose"],
                    "model": r["model"],
                    "batches": r["batches"],
                    "requests": r["requests"],
                    "oldest_hours": round((r["oldest_secs"] or 0) / 3600, 1),
                    "reason": r["reason"],
                },
            }
        )

    # Each section on its own: on 2026-09-04 three detectors failed in three
    # ways on one day, the exception took the whole task down, and the open
    # alerts auto-resolved because nothing re-observed them. A raising
    # detector looked exactly like all clear. Now it is an alert of its own.
    for section in (_detect_boards, _detect_queue, _detect_fleet, _detect_silent):
        try:
            found.extend(section())
        except Exception as exc:
            logger.exception(f"health detector {section.__name__} raised")
            found.append(
                {
                    "kind": "detector_failed",
                    "subject": section.__name__,
                    "severity": "critical",
                    "message": (
                        f"{section.__name__} raised {type(exc).__name__}: {str(exc)[:200]}. "
                        "Every alert it owns is unobserved until it runs again."
                    ),
                    "detail": {"error": str(exc)[:1000]},
                }
            )
    return found


# A worker whose last report is older than this is not idle, it is gone, and
# the reaper's concern rather than this detector's. Two housekeeping ticks.
WORKER_FRESH = "2 minutes"


def _detect_silent() -> list[dict[str, Any]]:
    """Work that reported success while doing nothing, from the audit of
    2026-09-05. Every check here is one indexed pass over tasks or the tiny
    health_alerts table, and every one can be made to fire in a test."""
    found: list[dict[str, Any]] = []

    # A sweep that finished done with work in front of it and none written.
    # comp and requirements used to count every line as done; now a line
    # counts only when its row lands, so this reads the honest number.
    for r in db.query(
        """
        SELECT kind, COUNT(*) AS n, MAX(id) AS task_id,
               MAX((progress->>'total')::int) AS total
        FROM tasks
        WHERE status = 'done' AND finished_at > now() - interval '24 hours'
          AND (progress->>'total')::int > 0 AND (progress->>'done')::int = 0
        GROUP BY kind
        """
    ):
        found.append(
            {
                "kind": "sweep_did_nothing",
                "subject": r["kind"],
                "severity": "warning",
                "message": (
                    f"{r['n']} {r['kind']} sweep(s) in 24h finished done with up to "
                    f"{r['total']} items in front of them and none completed (latest task "
                    f"{r['task_id']}). The work was selected, paid for if batched, and "
                    "nothing was written."
                ),
                "detail": dict(r),
            }
        )

    # A kind failing repeatedly. ingest_source has its own per-board and
    # per-host detectors; everything else failed in silence: a rotated
    # encryption key fails probe_credentials three times an hour with no
    # invalid_at and no alert.
    for r in db.query(
        """
        SELECT kind, COUNT(*) AS n, MAX(LEFT(error, 200)) AS sample_error
        FROM tasks
        WHERE status = 'failed' AND finished_at > now() - interval '3 hours'
          AND kind <> 'ingest_source'
        GROUP BY kind HAVING COUNT(*) >= 3
        """
    ):
        found.append(
            {
                "kind": "task_kind_failing",
                "subject": r["kind"],
                "severity": "critical",
                "message": (
                    f"{r['n']} {r['kind']} tasks failed in 3h; last error: "
                    f"{r['sample_error'] or ''}"
                ),
                "detail": dict(r),
            }
        )

    # A task the reaper keeps handing back. A graceful exit requeues with
    # attempts - 1, so a task that kills its worker every run never reaches
    # the attempt ceiling and never fails.
    for r in db.query(
        """
        SELECT id, kind, attempts, created_at
        FROM tasks
        WHERE status IN ('pending', 'running') AND attempts >= 3
          AND created_at < now() - interval '6 hours'
        """
    ):
        found.append(
            {
                "kind": "task_requeued_forever",
                "subject": str(r["id"]),
                "severity": "warning",
                "message": (
                    f"task {r['id']} ({r['kind']}) is on attempt {r['attempts']} and has "
                    f"been in the queue since {r['created_at']:%Y-%m-%d %H:%M}. Nothing "
                    "ends a task that keeps coming back."
                ),
                "detail": {"id": r["id"], "kind": r["kind"], "attempts": r["attempts"]},
            }
        )

    # An alert nobody was told about. _notify returns quietly when mail is not
    # configured or no admin has an address; notified_at was written but read
    # nowhere.
    from api import mail

    if mail.configured():
        r = db.query_one(
            """
            SELECT COUNT(*) AS n, MIN(first_seen) AS oldest
            FROM health_alerts
            WHERE resolved_at IS NULL AND notified_at IS NULL
              AND first_seen < now() - interval '1 hour'
            """
        )
        if r and r["n"]:
            found.append(
                {
                    "kind": "alerts_unnotified",
                    "subject": "_notify",
                    "severity": "warning",
                    "message": (
                        f"{r['n']} open alert(s) were never mailed, the oldest from "
                        f"{r['oldest']:%Y-%m-%d %H:%M}. Mail is configured, so the send "
                        "itself is failing or no admin has an address."
                    ),
                    "detail": {"n": r["n"]},
                }
            )
    return found


def _detect_fleet() -> list[dict[str, Any]]:
    """A live worker on a different release from the api.

    A roll recreates every container from one image; a host whose deploy
    never ran keeps heartbeating on the old one, with old code against the
    migrated database, and looks healthy on every other count. The api's
    own release is the reference because it rolls first. A worker whose
    process started before the api's own release was built is allowed the
    roll's own duration; past fleet_roll_minutes it is a host that did not
    deploy. Unknown releases (a local build) are not compared.
    """
    from api import telemetry

    if telemetry.RELEASE == "unknown":
        return []
    roll_minutes = int(db.get_config("fleet_roll_minutes"))
    return [
        {
            "kind": "fleet_mixed_release",
            "subject": r["name"],
            "severity": "critical",
            "message": (
                f"{r['name']} is running {r['release'] or 'an unknown release'} while the api "
                f"runs {telemetry.RELEASE}, and has been heartbeating on it for "
                f"{r['minutes']:.0f} minutes. A roll takes under {roll_minutes}; this host did "
                "not deploy, and its code is older than the database it is writing to."
            ),
            "detail": {
                "worker": r["name"],
                "worker_release": r["release"],
                "api_release": telemetry.RELEASE,
                "minutes": round(float(r["minutes"]), 1),
            },
        }
        for r in db.query(
            """
            SELECT name, release, EXTRACT(EPOCH FROM now() - started_at) / 60 AS minutes
            FROM worker_status
            WHERE last_seen > now() - %(fresh)s::interval
              AND release IS DISTINCT FROM %(api)s
              AND started_at < now() - make_interval(mins => %(roll)s)
            ORDER BY name
            """,
            {"fresh": WORKER_FRESH, "api": telemetry.RELEASE, "roll": roll_minutes},
        )
    ]


def _detect_queue() -> list[dict[str, Any]]:
    """The queue not moving when it should.

    Two shapes, each invisible from the totals on the dashboard: a worker
    reporting idle while pending work sits there (a claim takes one poll, so
    minutes of that is a stall, or a kinds allowlist that excludes what is
    queued), and pending ingests older than the cycle allows (the fleet is
    behind the hour and boards are going stale). Thresholds are persisted
    config, so an operator tunes them without a deploy.
    """
    found: list[dict[str, Any]] = []
    stall_minutes = int(db.get_config("queue_stall_minutes"))
    for r in db.query(
        """
        SELECT w.name, w.last_seen, o.n AS pending, o.kinds,
               EXTRACT(EPOCH FROM now() - o.at) / 60 AS oldest_minutes
        FROM worker_status w
        CROSS JOIN LATERAL (
            -- Per worker, not fleet-wide: a host that refuses ingest is not
            -- stalled by a queue full of ingest. The filters mirror
            -- _claim_task's, so this counts exactly what that worker would
            -- have taken had it been able to.
            SELECT MIN(t.created_at) AS at, COUNT(*) AS n,
                   array_agg(DISTINCT t.kind ORDER BY t.kind) AS kinds
            FROM tasks t
            WHERE t.status = 'pending'
              AND (cardinality(w.kinds) = 0 OR t.kind = ANY(w.kinds))
              AND NOT (t.kind = ANY(w.excluded_kinds))
        ) o
        WHERE w.current_task_id IS NULL
          AND w.last_seen > now() - %(fresh)s::interval
          AND o.at < now() - make_interval(mins => %(stall)s)
        """,
        {"fresh": WORKER_FRESH, "stall": stall_minutes},
    ):
        found.append(
            {
                "kind": "queue_stalled",
                "subject": r["name"],
                "severity": "critical",
                "message": (
                    f"{r['name']} has reported idle while {r['pending']} tasks sit pending, the "
                    f"oldest for {r['oldest_minutes']:.0f} minutes ({', '.join(r['kinds'])}). A "
                    f"claim takes one poll, and this count already excludes kinds this "
                    "worker refuses, so it cannot be explained by its kind filters."
                ),
                "detail": {
                    "worker": r["name"],
                    "pending": r["pending"],
                    "kinds": r["kinds"],
                    "oldest_minutes": round(float(r["oldest_minutes"]), 1),
                    "stall_minutes": stall_minutes,
                },
            }
        )

    from api.tasks.runtime import INGEST_INTERVAL_MINUTES

    cycles = int(db.get_config("ingest_backlog_cycles"))
    limit_minutes = cycles * INGEST_INTERVAL_MINUTES
    r = db.query_one(
        """
        SELECT COUNT(*) AS pending,
               EXTRACT(EPOCH FROM now() - MIN(created_at)) / 60 AS oldest_minutes,
               (SELECT COUNT(*) FROM tasks t2 WHERE t2.kind = 'ingest_source'
                  AND t2.finished_at > now() - interval '1 hour') AS done_last_hour
        FROM tasks WHERE kind = 'ingest_source' AND status = 'pending'
        """
    )
    if r and r["pending"] and float(r["oldest_minutes"] or 0) > limit_minutes:
        found.append(
            {
                "kind": "ingest_backlog",
                "subject": "ingest_source",
                "severity": "warning",
                "message": (
                    f"{r['pending']} ingests are pending and the oldest has waited "
                    f"{r['oldest_minutes']:.0f} minutes, past {cycles} cycles of "
                    f"{INGEST_INTERVAL_MINUTES}. The fleet finished {r['done_last_hour']} in the "
                    "last hour; at that rate the pile is what the number says it is."
                ),
                "detail": {
                    "pending": r["pending"],
                    "oldest_minutes": round(float(r["oldest_minutes"]), 1),
                    "done_last_hour": r["done_last_hour"],
                    "limit_minutes": limit_minutes,
                },
            }
        )
    return found


# How many consecutive failed ingests mean a board is broken rather than
# unlucky. Measured over the week to 2026-09-04: no source failed more than
# once, and every failure was transient (a closed connection during a roll,
# a thread limit). Three in a row is three hours of the same thing.
INGEST_FAILURE_STREAK = 3

# Below this many page fetches a worker's failure rate is noise.
MIN_FETCH_SAMPLES = 20


def _detect_boards() -> list[dict[str, Any]]:
    """The ways a board stops delivering without anything reporting an error.

    Each reads the counts an ingest leaves on its task (fetched, kept, cached,
    fetch_failed), which is the only record of what one fetch of a feed saw.
    A board can be 'done' every hour and contribute nothing: the feed moved
    and returns an empty table, the title pattern admits no title, a worker's
    IP is being refused. The admin list shows last_new_posting_at, which goes
    quiet for a healthy mirror too, so it cannot tell these apart.
    """
    found: list[dict[str, Any]] = []

    # 1. The fetch itself failing, hour after hour. One failure is a blip;
    #    the hourly cycle is its retry.
    for r in db.query(
        """
        WITH recent AS (
            SELECT payload->>'source' AS source, status, error,
                   row_number() OVER (PARTITION BY payload->>'source' ORDER BY id DESC) AS rn
            FROM tasks
            WHERE kind = 'ingest_source' AND status IN ('done', 'failed')
              AND created_at > now() - interval '3 days'
        )
        SELECT r.source, max(r.error) AS error
        FROM recent r JOIN sources s ON s.name = r.source AND s.active
        WHERE r.rn <= %(streak)s
        GROUP BY r.source
        HAVING count(*) = %(streak)s AND count(*) FILTER (WHERE r.status = 'failed') = %(streak)s
        """,
        {"streak": INGEST_FAILURE_STREAK},
    ):
        found.append(
            {
                "kind": "ingest_failing",
                "subject": r["source"],
                "severity": "critical",
                "message": (
                    f"the last {INGEST_FAILURE_STREAK} ingests of {r['source']} all failed; "
                    f"last error: {(r['error'] or '')[:160]}"
                ),
                "detail": {"source": r["source"], "error": r["error"]},
            }
        )

    # 2. The fetch succeeding and returning nothing, or a title pattern that
    #    admits nothing. Only for a board that has produced before: a board
    #    that never has is 'never produced' on the admin list, not an alert.
    for r in db.query(
        """
        WITH ingests AS (
            SELECT payload->>'source' AS source, id, finished_at,
                   (progress->>'fetched')::int AS fetched,
                   (progress->>'kept')::int AS kept
            FROM tasks
            WHERE kind = 'ingest_source' AND status = 'done'
              AND progress ? 'fetched'
              AND created_at > now() - interval '8 days'
        ),
        latest AS (
            SELECT DISTINCT ON (source) source, fetched, kept, finished_at
            FROM ingests ORDER BY source, finished_at DESC
        )
        SELECT l.source, l.fetched, l.kept, l.finished_at, s.title_pattern,
               (SELECT max(i.fetched) FROM ingests i
                WHERE i.source = l.source AND i.finished_at < now() - interval '24 hours')
                   AS best_prior_fetched,
               (SELECT max(i.kept) FROM ingests i
                WHERE i.source = l.source AND i.finished_at < now() - interval '24 hours')
                   AS best_prior_kept
        FROM latest l JOIN sources s ON s.name = l.source AND s.active
        WHERE l.finished_at > now() - interval '24 hours'
        """
    ):
        if r["fetched"] == 0 and (r["best_prior_fetched"] or 0) > 0:
            found.append(
                {
                    "kind": "source_feed_empty",
                    "subject": r["source"],
                    "severity": "critical",
                    "message": (
                        f"{r['source']} fetched fine and returned 0 postings; it returned "
                        f"{r['best_prior_fetched']} within the prior week. The feed moved, "
                        "the board token changed, or the table shape did."
                    ),
                    "detail": dict(r),
                }
            )
        elif r["fetched"] >= 50 and r["kept"] == r["fetched"] and r["title_pattern"]:
            found.append(
                {
                    "kind": "source_pattern_admits_all",
                    "subject": r["source"],
                    "severity": "warning",
                    "message": (
                        f"{r['source']} returned {r['fetched']} postings and its title "
                        "pattern admitted every one. A pattern that drops nothing on a "
                        "company board is a pattern that stopped matching, and every "
                        "admitted posting is paid for."
                    ),
                    "detail": dict(r),
                }
            )
        elif (
            r["fetched"] > 0
            and r["kept"] == 0
            and r["title_pattern"]
            # A board that admitted one or two roles last week and none this
            # week has had them filled, not its pattern broken. At 2,293
            # boards that churn held 18 warnings open at once (2026-09-05).
            and (r["best_prior_kept"] or 0) >= 5
        ):
            found.append(
                {
                    "kind": "source_pattern_excludes_all",
                    "subject": r["source"],
                    "severity": "warning",
                    "message": (
                        f"{r['source']} returned {r['fetched']} postings and its title "
                        f"pattern admitted none; it admitted {r['best_prior_kept']} within "
                        "the prior week."
                    ),
                    "detail": dict(r),
                }
            )

    # Ingests failing against one upstream host, whichever boards they were.
    # 143 Workable boards failed on one per-address limit on 2026-09-05 and
    # each looked like its own transient failure; the host was the story.
    for r in db.query(
        """
        SELECT substring(s.listings_url from '//([^/]+)') AS host,
               COUNT(*) AS recent_total,
               COUNT(*) FILTER (WHERE t.status = 'failed') AS recent_failed,
               COUNT(DISTINCT t.payload->>'source') FILTER (WHERE t.status = 'failed')
                   AS boards_failed,
               max(t.error) FILTER (WHERE t.status = 'failed') AS sample_error
        FROM tasks t JOIN sources s ON s.name = t.payload->>'source'
        WHERE t.kind = 'ingest_source' AND t.status IN ('done', 'failed')
          AND t.created_at > now() - interval '24 hours'
        GROUP BY 1
        """
    ):
        if r["recent_failed"] < 10 or _pct(r["recent_failed"], r["recent_total"]) < 0.25:
            continue
        found.append(
            {
                "kind": "ingest_host_failing",
                "subject": r["host"],
                "severity": "warning",
                "message": (
                    f"{r['recent_failed']} of {r['recent_total']} ingests against {r['host']} "
                    f"failed in 24h across {r['boards_failed']} boards; last error: "
                    f"{(r['sample_error'] or '')[:120]}"
                ),
                "detail": dict(r),
            }
        )

    # 3. One worker's page fetches failing where they used to land: the
    #    egress-blocked signature, per fleet host rather than per site.
    #    Relative to the worker's own prior week, like extraction_failing.
    for r in db.query(
        """
        SELECT worker,
               COALESCE(sum((progress->>'cached')::int + (progress->>'fetch_failed')::int)
                   FILTER (WHERE finished_at > now() - interval '24 hours'), 0) AS recent_total,
               COALESCE(sum((progress->>'fetch_failed')::int)
                   FILTER (WHERE finished_at > now() - interval '24 hours'), 0) AS recent_failed,
               COALESCE(sum((progress->>'cached')::int + (progress->>'fetch_failed')::int)
                   FILTER (WHERE finished_at BETWEEN now() - interval '8 days'
                           AND now() - interval '24 hours'), 0) AS base_total,
               COALESCE(sum((progress->>'fetch_failed')::int)
                   FILTER (WHERE finished_at BETWEEN now() - interval '8 days'
                           AND now() - interval '24 hours'), 0) AS base_failed
        FROM tasks
        WHERE kind = 'ingest_source' AND status = 'done' AND worker IS NOT NULL
          AND progress ? 'fetch_failed'
          AND created_at > now() - interval '8 days'
        GROUP BY worker
        """
    ):
        if r["recent_total"] < MIN_FETCH_SAMPLES or r["base_total"] < MIN_FETCH_SAMPLES:
            continue
        recent = _pct(r["recent_failed"], r["recent_total"])
        base = _pct(r["base_failed"], r["base_total"])
        if recent - base >= 0.25 and recent >= 0.5:
            found.append(
                {
                    "kind": "worker_fetches_failing",
                    "subject": r["worker"],
                    "severity": "warning",
                    "message": (
                        f"{r['recent_failed']} of {r['recent_total']} page fetches on "
                        f"{r['worker']} failed in 24h ({recent:.0%}, up from {base:.0%} over "
                        "the prior week) across every board it ingested. The other workers "
                        "are the control: if they are fine, this host's egress is being refused."
                    ),
                    "detail": dict(r),
                }
            )

    # 4. One applicant-tracking system's API path going quiet. When a resolver
    #    stops returning text we fall back to chromium for that system's
    #    postings, and ats_text_collapse only sees it per source, where an
    #    aggregator mixes five systems. Grouped by system, a broken resolver is
    #    one row.
    for r in db.query(
        """
        SELECT CASE
                 WHEN url LIKE '%%greenhouse.io%%' OR url LIKE '%%gh_jid=%%' THEN 'greenhouse'
                 WHEN url LIKE '%%lever.co%%' THEN 'lever'
                 WHEN url LIKE '%%ashbyhq.com%%' THEN 'ashby'
                 WHEN url LIKE '%%myworkdayjobs.com%%' THEN 'workday'
                 WHEN url LIKE '%%smartrecruiters.com%%' THEN 'smartrecruiters'
               END AS ats,
               COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours') AS recent_total,
               COUNT(*) FILTER (WHERE created_at > now() - interval '24 hours'
                                AND reason = 'ats text') AS recent_ats,
               COUNT(*) FILTER (WHERE created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours') AS base_total,
               COUNT(*) FILTER (WHERE created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours' AND reason = 'ats text') AS base_ats
        FROM ai_queries
        WHERE check_type = 'content' AND reason IN ('ats text', 'scraped', 'static')
          AND created_at > now() - interval '8 days'
        GROUP BY 1
        HAVING CASE
                 WHEN url LIKE '%%greenhouse.io%%' OR url LIKE '%%gh_jid=%%' THEN 'greenhouse'
                 WHEN url LIKE '%%lever.co%%' THEN 'lever'
                 WHEN url LIKE '%%ashbyhq.com%%' THEN 'ashby'
                 WHEN url LIKE '%%myworkdayjobs.com%%' THEN 'workday'
                 WHEN url LIKE '%%smartrecruiters.com%%' THEN 'smartrecruiters'
               END IS NOT NULL
        """
    ):
        if r["recent_total"] < MIN_CONTENT_SAMPLES or r["base_total"] < MIN_CONTENT_SAMPLES:
            continue
        recent = _pct(r["recent_ats"], r["recent_total"])
        base = _pct(r["base_ats"], r["base_total"])
        if base >= 0.5 and base - recent >= 0.4:
            found.append(
                {
                    "kind": "resolver_bypassed",
                    "subject": r["ats"],
                    "severity": "warning",
                    "message": (
                        f"{r['ats']} postings came from its API {recent:.0%} of the time in "
                        f"24h, down from {base:.0%} over the prior week ({r['recent_total']} "
                        "postings). The resolver is failing and every one of them is going "
                        "through the browser instead."
                    ),
                    "detail": dict(r),
                }
            )

    return found


def record(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Upserts open alerts and auto-resolves ones that stopped firing. Returns
    only the newly-opened alerts, so notification never repeats for a condition
    that is merely still true."""
    seen = {(f["kind"], f["subject"]) for f in found}
    fresh: list[dict[str, Any]] = []
    for f in found:
        row = db.query_one(
            """
            INSERT INTO health_alerts (kind, subject, severity, message, detail)
            VALUES (%(kind)s, %(subject)s, %(severity)s, %(message)s, %(detail)s)
            ON CONFLICT (kind, subject) WHERE resolved_at IS NULL
            DO UPDATE SET last_seen = now(), message = EXCLUDED.message,
                          detail = EXCLUDED.detail, severity = EXCLUDED.severity
            RETURNING id, (xmax = 0) AS is_new
            """,
            {
                "kind": f["kind"],
                "subject": f["subject"],
                "severity": f["severity"],
                "message": f["message"],
                "detail": db.jsonb(f["detail"]),
            },
        )
        if row and row["is_new"]:
            fresh.append({**f, "id": row["id"]})
            # The condition's timeline beside the raw failures: a detector
            # opening is an event too, so the error tracker can show what
            # was wrong when the tracebacks started.
            telemetry.capture(
                "alert_opened",
                properties={
                    "kind": f["kind"],
                    "subject": f["subject"],
                    "severity": f["severity"],
                    "alert_id": row["id"],
                },
            )

    # Resolve only after RESOLVE_GRACE of silence. A detector goes quiet when
    # it stops being evaluable as readily as when the condition ends, and
    # resolving on the first miss turns that into an alert that reopens next
    # hour and mails again. last_seen is refreshed by the upsert above, so a
    # condition that is still firing never ages into this.
    open_rows = db.query(
        "SELECT id, kind, subject FROM health_alerts WHERE resolved_at IS NULL "
        "AND last_seen < now() - %s::interval",
        (RESOLVE_GRACE,),
    )
    stale = [r for r in open_rows if (r["kind"], r["subject"]) not in seen]
    if stale:
        db.execute(
            "UPDATE health_alerts SET resolved_at = now() WHERE id = ANY(%s)",
            ([r["id"] for r in stale],),
        )
        for r in stale:
            telemetry.capture(
                "alert_resolved",
                properties={"kind": r["kind"], "subject": r["subject"], "alert_id": r["id"]},
            )
    return fresh
