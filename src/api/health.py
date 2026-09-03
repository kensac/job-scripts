from __future__ import annotations

import logging
from typing import Any

from api import db

logger = logging.getLogger("jobtracker_health")

# Minimum sample sizes: below these a "rate" is noise, and alerting on noise
# trains you to ignore alerts. The rate floor is deliberately high because the
# samples are not independent — see MAX_PER_COMPANY.
MIN_SAMPLES = 50
MIN_CONTENT_SAMPLES = 10

# One employer bulk-posting a batch of no-sponsorship roles is a single
# editorial decision, not N independent observations. Capping each company's
# contribution per window keeps a seasonal drop (Qorvo posted 36 internships in
# one day, 31 of them clearance-restricted) from moving a whole source's rate.
MAX_PER_COMPANY = 5

# A job whose first-ever check lands long after we catalogued it came from a
# backlog sweep, not the live feed. Those are systematically staler — and more
# often closed or restricted — than freshly-ingested postings, so mixing them
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


def _pct(part: int, whole: int) -> float:
    return (part / whole) if whole else 0.0


def detect() -> list[dict[str, Any]]:
    """Compares the last 24h against the preceding week, per source, looking for
    the shapes that mean 'something upstream changed' rather than 'the job
    market moved'. Everything here is deliberately relative to each source's
    own baseline — absolute thresholds would fire constantly on sources that
    are legitimately mostly-closed or legitimately short."""
    found: list[dict[str, Any]] = []

    # 1. The ATS text path silently breaking. When a resolver stops returning
    #    usable text we fall back to chromium, so the share collapses long
    #    before anything is visibly wrong — this is the earliest warning we get.
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
          -- share far below the `base >= 0.30` floor — which is why this
          -- detector had never once fired.
          AND q.reason IN ('ats text', 'scraped')
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
                        f"ATS text share for {r['source']} fell from {base:.0%} to {recent:.0%} "
                        "— the resolver is probably broken and we're paying to scrape instead."
                    ),
                    "detail": dict(r),
                }
            )

    # 2. Verdict-rate shifts on FIRST-EVER checks only. Re-checks flipping to
    #    closed is the system working — postings expire, and a sweep that newly
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
                        f"first-time checks (max {MAX_PER_COMPANY} per company) — jobs are "
                        "being written off on arrival, so suspect the input text before "
                        "believing the verdicts."
                    ),
                    "detail": dict(r),
                }
            )

    # 3. Extraction failures concentrated on one host: the bot-wall signature.
    #    Relative to the host's own prior week, not an absolute rate — a site
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
                        f"failed in 24h ({recent:.0%}, up from {base:.0%} over the prior week) "
                        "— blocked, or the page shape changed."
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
                    f"{r['dead_for']} ago ({r['invalid_reason']}) — mail ingest is "
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
    stale = [r["id"] for r in open_rows if (r["kind"], r["subject"]) not in seen]
    if stale:
        db.execute("UPDATE health_alerts SET resolved_at = now() WHERE id = ANY(%s)", (stale,))
    return fresh
