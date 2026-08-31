from __future__ import annotations

import logging
from typing import Any, Dict, List

from api import db

logger = logging.getLogger("jobtracker_health")

# Minimum sample sizes: below these a "rate" is noise, and alerting on noise
# trains you to ignore alerts.
MIN_SAMPLES = 15
MIN_CONTENT_SAMPLES = 10


def _pct(part: int, whole: int) -> float:
    return (part / whole) if whole else 0.0


def backfill_distorting() -> bool:
    """The content backfill scrapes jobs that never had text, which inflates the
    'scraped' share and reads exactly like an ATS resolver breaking. Only the
    ATS detector is gated on this — the rejection-rate and fetch-failure
    detectors aren't distorted the same way, and over-suppression is its own
    way of going blind. Self-clearing: once the backlog drains the task stops
    doing work and the detector comes back."""
    return db.query_one(
        "SELECT 1 FROM tasks WHERE kind = 'fetch_missing_content' "
        "AND finished_at > now() - interval '24 hours' "
        "AND COALESCE((progress->>'total')::int, 0) > 0 LIMIT 1"
    ) is not None


def detect() -> List[Dict[str, Any]]:
    """Compares the last 24h against the preceding week, per source, looking for
    the shapes that mean 'something upstream changed' rather than 'the job
    market moved'. Everything here is deliberately relative to each source's
    own baseline — absolute thresholds would fire constantly on sources that
    are legitimately mostly-closed or legitimately short."""
    found: List[Dict[str, Any]] = []
    ats_gated = backfill_distorting()

    # 1. The ATS text path silently breaking. When a resolver stops returning
    #    usable text we fall back to chromium, so the share collapses long
    #    before anything is visibly wrong — this is the earliest warning we get.
    for r in db.query(
        """
        SELECT j.source,
               COUNT(*) FILTER (WHERE q.created_at::timestamptz > now() - interval '24 hours') AS recent_total,
               COUNT(*) FILTER (WHERE q.created_at::timestamptz > now() - interval '24 hours'
                                AND q.reason = 'ats text') AS recent_ats,
               COUNT(*) FILTER (WHERE q.created_at::timestamptz BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours') AS base_total,
               COUNT(*) FILTER (WHERE q.created_at::timestamptz BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours' AND q.reason = 'ats text') AS base_ats
        FROM ai_queries q JOIN jobs j ON j.url = q.url
        WHERE q.check_type = 'content'
          AND q.created_at::timestamptz > now() - interval '8 days'
        GROUP BY j.source
        """
    ):
        if r["recent_total"] < MIN_CONTENT_SAMPLES or r["base_total"] < MIN_CONTENT_SAMPLES:
            continue
        recent = _pct(r["recent_ats"], r["recent_total"])
        base = _pct(r["base_ats"], r["base_total"])
        # Only meaningful if the source actually relied on ATS text before.
        if ats_gated:
            continue
        if base >= 0.30 and recent < base * 0.5:
            found.append({
                "kind": "ats_text_collapse",
                "subject": r["source"],
                "severity": "critical",
                "message": (
                    f"ATS text share for {r['source']} fell from {base:.0%} to {recent:.0%} "
                    "— the resolver is probably broken and we're paying to scrape instead."
                ),
                "detail": dict(r),
            })

    # 2. Verdict-rate shifts on FIRST-EVER checks only. Re-checks flipping to
    #    closed is the system working — postings expire, and a sweep that newly
    #    covers a backlog of old jobs will legitimately reject a lot of them at
    #    once. Mixing the two makes coverage changes look like breakage (it did:
    #    the first live alert was a reverify backlog on a board whose postings
    #    expire in ~2 days). What genuinely indicates something upstream broke
    #    is FRESHLY-SEEN jobs being classified closed at an unusual rate.
    for r in db.query(
        """
        SELECT j.source, q.check_type,
               COUNT(*) FILTER (WHERE q.created_at::timestamptz > now() - interval '24 hours') AS recent_total,
               COUNT(*) FILTER (WHERE q.created_at::timestamptz > now() - interval '24 hours'
                                AND q.status = 'rejected') AS recent_rejected,
               COUNT(*) FILTER (WHERE q.created_at::timestamptz BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours') AS base_total,
               COUNT(*) FILTER (WHERE q.created_at::timestamptz BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours' AND q.status = 'rejected') AS base_rejected
        FROM ai_queries q JOIN jobs j ON j.url = q.url
        WHERE q.check_type IN ('closed', 'clearance')
          AND q.status IN ('passed', 'rejected')
          AND q.created_at::timestamptz > now() - interval '8 days'
          AND NOT EXISTS (
            SELECT 1 FROM ai_queries p
            WHERE p.url = q.url AND p.check_type = q.check_type
              AND p.id < q.id AND p.status IN ('passed', 'rejected'))
        GROUP BY j.source, q.check_type
        """
    ):
        if r["recent_total"] < MIN_SAMPLES or r["base_total"] < MIN_SAMPLES:
            continue
        recent = _pct(r["recent_rejected"], r["recent_total"])
        base = _pct(r["base_rejected"], r["base_total"])
        if recent - base >= 0.25 and recent >= 0.35:
            found.append({
                "kind": f"{r['check_type']}_rate_spike",
                "subject": r["source"],
                "severity": "critical",
                "message": (
                    f"{r['check_type']} rejection rate for newly-seen {r['source']} jobs "
                    f"jumped from {base:.0%} to {recent:.0%} over {r['recent_total']} "
                    "first-time checks — jobs are being written off on arrival, so "
                    "suspect the input text before believing the verdicts."
                ),
                "detail": dict(r),
            })

    # 3. Extraction failures concentrated on one host: the bot-wall signature.
    for r in db.query(
        """
        SELECT substring(url from '//([^/]+)') AS host,
               COUNT(*) FILTER (WHERE status = 'failed') AS failures,
               COUNT(*) AS total
        FROM ai_queries
        WHERE check_type IN ('extraction', 'content')
          AND created_at::timestamptz > now() - interval '24 hours'
        GROUP BY 1 HAVING COUNT(*) >= 20
        """
    ):
        rate = _pct(r["failures"], r["total"])
        if rate >= 0.5:
            found.append({
                "kind": "extraction_failing",
                "subject": r["host"],
                "severity": "warning",
                "message": (
                    f"{r['failures']} of {r['total']} fetches from {r['host']} failed in 24h "
                    f"({rate:.0%}) — blocked, or the page shape changed."
                ),
                "detail": dict(r),
            })

    return found


def record(found: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Upserts open alerts and auto-resolves ones that stopped firing. Returns
    only the newly-opened alerts, so notification never repeats for a condition
    that is merely still true."""
    seen = {(f["kind"], f["subject"]) for f in found}
    fresh: List[Dict[str, Any]] = []
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
                "kind": f["kind"], "subject": f["subject"], "severity": f["severity"],
                "message": f["message"], "detail": db.jsonb(f["detail"]),
            },
        )
        if row and row["is_new"]:
            fresh.append({**f, "id": row["id"]})

    open_rows = db.query(
        "SELECT id, kind, subject FROM health_alerts WHERE resolved_at IS NULL"
    )
    stale = [r["id"] for r in open_rows if (r["kind"], r["subject"]) not in seen]
    if stale:
        db.execute(
            "UPDATE health_alerts SET resolved_at = now() WHERE id = ANY(%s)", (stale,)
        )
    return fresh
