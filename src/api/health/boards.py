"""The boards: whether a feed still returns what it used to."""

from __future__ import annotations

import logging
from typing import Any

from api import db, queue
from api.health.evidence import (
    MIN_CONTENT_SAMPLES,
    _int_from,
    _pct,
)

logger = logging.getLogger(__name__)
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

    # 1. The fetch itself failing, pull after pull. One failure is a blip.
    #    Read as a run rather than a window, because a failing board backs
    #    off (queue.backing_off) and a daily board's third failure lands three
    #    days after its first. A board that ran into
    #    ingest_give_up_after_failures has been switched off by its last pull
    #    (tasks.ingest), and says so until someone switches it back on or
    #    deletes it: its postings are retired, which nobody should learn
    #    from a board going quiet.
    give_up = int(db.get_config("ingest_give_up_after_failures"))
    for name, r in sorted(queue.failure_runs(at_least=INGEST_FAILURE_STREAK).items()):
        error = (r["last_error"] or "")[:160]
        detail = {
            "source": name,
            "failures": r["failures"],
            "last_failed": r["last_failed"].isoformat(),
            "error": r["last_error"],
        }
        if r["active"]:
            found.append(
                {
                    "kind": "ingest_failing",
                    "subject": name,
                    "severity": "critical",
                    "message": (
                        f"the last {r['failures']} ingests of {name} all failed; it is "
                        f"switched off at {give_up}. Last error: {error}"
                    ),
                    "detail": detail,
                }
            )
        elif r["failures"] >= give_up:
            found.append(
                {
                    "kind": "source_switched_off",
                    "subject": name,
                    "severity": "warning",
                    "message": (
                        f"{name} is switched off and its last {r['failures']} pulls failed, "
                        f"the last at {r['last_failed']:%Y-%m-%d %H:%M} UTC, so its postings "
                        f"are retired. Last error: {error}. Fix its listings URL and switch "
                        "it back on, or delete it."
                    ),
                    "detail": detail,
                }
            )

    # 1b. A board that pulls fine and has never listed a posting. Not a
    #     failure: an empty pull may be a company with no open roles, and
    #     switching it off would miss the day it posts one, for about one
    #     request a day. But pulled forever unseen, it is a slug that never
    #     matched anything; 105 such boards on 2026-10-04. One alert for the
    #     set, since each is a cleanup and not an incident.
    never = [
        r["source"]
        for r in db.query(
            f"""
            WITH pulls AS (
                SELECT payload->>'source' AS source,
                       max({_int_from("progress", "fetched")}) AS most
                FROM tasks
                WHERE kind = 'ingest_source' AND status = 'done' AND progress ? 'fetched'
                  AND created_at > now() - interval '8 days'
                GROUP BY 1
            )
            SELECT p.source FROM pulls p JOIN sources s ON s.name = p.source AND s.active
            WHERE p.most = 0 AND s.created_at < now() - interval '7 days'
              AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.source = p.source)
              AND NOT EXISTS (SELECT 1 FROM listings l WHERE l.source = p.source)
            ORDER BY p.source
            """
        )
    ]
    if never:
        found.append(
            {
                "kind": "sources_never_produced",
                "subject": "boards",
                "severity": "warning",
                "message": (
                    f"{len(never)} switched-on boards older than a week pulled fine for the "
                    "last 8 days, listed nothing, and have never put a posting in the "
                    f"catalog: {', '.join(never[:10])}{', ...' if len(never) > 10 else ''}. "
                    "Check each listings URL; an empty board may only have no open roles."
                ),
                "detail": {"sources": never},
            }
        )

    # 2. The fetch succeeding and returning nothing, or a title pattern that
    #    admits nothing. Only for a board that has produced before: a board
    #    that never has is 'never produced' on the admin list, not an alert.
    for r in db.query(
        f"""
        WITH ingests AS (
            SELECT payload->>'source' AS source, id, finished_at,
                   {_int_from("progress", "fetched")} AS fetched,
                   {_int_from("progress", "kept")} AS kept,
                   COALESCE((progress->>'pattern_enforced')::boolean, true)
                       AS pattern_enforced
            FROM tasks
            WHERE kind = 'ingest_source' AND status = 'done'
              AND progress ? 'fetched'
              AND created_at > now() - interval '8 days'
        ),
        latest AS (
            SELECT DISTINCT ON (source) source, fetched, kept, pattern_enforced, finished_at
            FROM ingests ORDER BY source, finished_at DESC
        )
        SELECT l.source, l.fetched, l.kept, l.pattern_enforced, l.finished_at, s.title_pattern,
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
            prior = r["best_prior_fetched"]
            found.append(
                {
                    "kind": "source_feed_empty",
                    "subject": r["source"],
                    "severity": "warning",
                    "message": (
                        f"{r['source']} fetched fine and returned 0 postings; it returned "
                        f"{prior} within the prior week. This may mean there are no open "
                        "roles, or the feed or parser changed; emptiness alone cannot distinguish them."
                    ),
                    "detail": dict(r),
                }
            )
        elif (
            r["pattern_enforced"]
            and r["fetched"] >= 50
            and r["kept"] == r["fetched"]
            and r["title_pattern"]
        ):
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
            and r["pattern_enforced"]
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
        f"""
        SELECT worker,
               COALESCE(sum(COALESCE({_int_from("progress", "cached")}, 0) + COALESCE({_int_from("progress", "fetch_failed")}, 0))
                   FILTER (WHERE finished_at > now() - interval '24 hours'), 0) AS recent_total,
               COALESCE(sum({_int_from("progress", "fetch_failed")})
                   FILTER (WHERE finished_at > now() - interval '24 hours'), 0) AS recent_failed,
               COALESCE(sum(COALESCE({_int_from("progress", "cached")}, 0) + COALESCE({_int_from("progress", "fetch_failed")}, 0))
                   FILTER (WHERE finished_at BETWEEN now() - interval '8 days'
                           AND now() - interval '24 hours'), 0) AS base_total,
               COALESCE(sum({_int_from("progress", "fetch_failed")})
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
                                AND reason IN ('ats text', 'listing text')) AS recent_ats,
               COUNT(*) FILTER (WHERE created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours') AS base_total,
               COUNT(*) FILTER (WHERE created_at BETWEEN now() - interval '8 days'
                                AND now() - interval '24 hours'
                                AND reason IN ('ats text', 'listing text')) AS base_ats
        FROM ai_queries
        WHERE check_type = 'content'
          AND reason IN ('ats text', 'listing text', 'scraped', 'static')
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
