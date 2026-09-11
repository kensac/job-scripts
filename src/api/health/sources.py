"""Per source, the last 24h against the preceding week."""

from __future__ import annotations

import logging
from typing import Any

from api import db
from api.health.evidence import (
    FRESH_CHECK_WINDOW,
    MAX_PER_COMPANY,
    MIN_CONTENT_SAMPLES,
    MIN_SAMPLES,
    _pct,
)
from core.checks import POSTING_CHECK_NAMES

logger = logging.getLogger(__name__)


def _detect_sources() -> list[dict[str, Any]]:
    """The per-source comparisons: ATS text share, first-check verdict rates,
    extraction failures concentrated on one host, and the batch failures that
    close clean.

    A section rather than code inline in detect(), because inline is what it
    was and inline meant unprotected. The loop in detect() wrapped the four
    extracted detectors; these ran ahead of it, so one of them raising took the
    whole hourly run down and every open alert auto-resolved with nothing left
    to re-observe it. That is the exact failure the loop was added for on
    2026-09-04, and it never covered these.
    """
    found: list[dict[str, Any]] = []

    # 1. The ATS text path silently breaking. When a resolver stops returning
    #    usable text we fall back to chromium, so the share collapses long
    #    before anything is visibly wrong. This is the earliest warning we get.
    for r in db.query(
        """
        SELECT j.source,
               COUNT(*) FILTER (WHERE q.created_at > now() - interval '24 hours') AS recent_total,
               -- The board's own API text, whether the listing call carried
               -- it ("listing text", since #331) or the resolver fetched it
               -- ("ats text"). Counting only the resolver read the listing
               -- path taking over as a collapse: gh_point72, 98 to 47 percent,
               -- 2026-09-06, with 58 of 100 rows being listing text.
               COUNT(*) FILTER (WHERE q.created_at > now() - interval '24 hours'
                                AND q.reason IN ('ats text', 'listing text')) AS recent_ats,
               COUNT(*) FILTER (WHERE q.created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours') AS base_total,
               COUNT(*) FILTER (WHERE q.created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours'
                                AND q.reason IN ('ats text', 'listing text')) AS base_ats
        FROM ai_queries q JOIN jobs j ON j.url = q.url
        WHERE q.check_type = 'content'
          -- Only rows that record where the text CAME from. Other writers
          -- (pittcsc's 'content cached') log a content row with no origin,
          -- and counting those in the denominator silently buries the ATS
          -- share far below the `base >= 0.30` floor, which is why this
          -- detector had never once fired.
          AND q.reason IN ('ats text', 'listing text', 'scraped', 'static')
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
            WHERE q.check_type = ANY(%(posting_checks)s)
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
        {
            "fresh_window": FRESH_CHECK_WINDOW,
            "cap": MAX_PER_COMPANY,
            "posting_checks": list(POSTING_CHECK_NAMES),
        },
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

    return found
