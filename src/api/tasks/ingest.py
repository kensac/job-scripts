"""Source ingest: fetch a board's postings and cache their pages.

Deliberately runs no AI - it arrives through the task queue, which makes it
scheduled work, and scheduled work batches.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlparse

from api import db, metrics, telemetry, verdicts
from api.tasks.board import _content_attempted_urls, _content_ready_urls
from api.tasks.runtime import _cancelled, _set_progress, enqueue
from core.store import add_ai_result

logger = logging.getLogger("jobtracker_worker")


async def handle_ingest_source(task_id: int, payload: dict[str, Any]) -> None:
    from core import boards, catalog
    from core.pittcsc_simplify import FALLBACK_CUTOFF_TS

    source = db.query_one("SELECT * FROM sources WHERE name = %s AND active", (payload["source"],))
    if not source:
        raise LookupError("unknown or inactive source")

    try:
        postings = await asyncio.to_thread(
            boards.fetch_listings, source["listings_url"], source["company"]
        )
    except Exception as exc:
        # The board itself failed to answer: the source, the host and the
        # HTTP status when there was one, so a board going dark is a query
        # rather than a traceback search.
        response = getattr(exc, "response", None)
        telemetry.capture(
            "ingest_pull_failed",
            properties={
                "source": source["name"],
                "fetch_host": urlparse(source["listings_url"]).netloc.lower(),
                "status": getattr(response, "status_code", None),
                "error_class": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )
        raise
    fetched = len(postings)
    listed = postings
    if source["title_pattern"]:
        keep = re.compile(source["title_pattern"], re.IGNORECASE)
        postings = [p for p in postings if keep.search(p.title)]
    # Everything the board returned stays on record, admitted or not, with
    # the text the listing carried: a candidate pattern is judged against the
    # titles the board actually listed, a posting a better pattern admits
    # arrives here on the next pull, and a backfill reads the text from here
    # instead of scraping the page again.
    catalog.record_listings(
        listed,
        source["name"],
        source["title_pattern"] or "",
        {p.url for p in postings},
        int(db.get_config("screened_retention_days")),
    )
    upserted = catalog.upsert_postings(postings, source["name"])
    # A company board lists every open posting, so a catalog row this pull
    # did not admit is closed (the board dropped it) or retired (the pattern
    # no longer admits it); either way it stops costing checks and leaves
    # boards through _demote_closed. Listed and admitted again, the upsert
    # above sets it active. Not for aggregator lists, whose rows age off on
    # their own schedule, and not on an empty pull, which is a broken fetch
    # rather than an empty board. Nothing did this before: 4,554 of 6,306
    # active company-board rows on 2026-09-04 were titles the pattern no
    # longer admitted, each still eligible for every sweep.
    if fetched and boards.kind(source["listings_url"]) in boards.AUTHORITATIVE:
        retired = catalog.retire_unlisted(source["name"], [p.url for p in postings])
        metrics.INGEST_JOBS.labels(source["name"], "retired").inc(retired)
    else:
        retired = 0
    metrics.INGEST_JOBS.labels(source["name"], "fetched").inc(fetched)
    metrics.INGEST_JOBS.labels(source["name"], "title_excluded").inc(fetched - len(postings))
    metrics.INGEST_JOBS.labels(source["name"], "upserted").inc(upserted)
    logger.info(
        f"Ingest {source['name']}: fetched {fetched}, "
        f"title excluded {fetched - len(postings)}, upserted {upserted}"
    )

    # Ingest caches pages but runs NO AI. It reached here through the task
    # queue, which makes it scheduled work, and the rule for scheduled work is
    # that it batches: the hourly verify_new task settles closed+clearance as
    # one half-price call per job. Checking inline here bypassed that. It was
    # why ~90% of closed/clearance verdicts were still full-price sync calls,
    # since verify_new only ever saw the jobs ingest had not already reached.
    #
    # Caching the page stays here on purpose. verify_new can only batch a job
    # whose text is already stored, and leaving that to fetch_missing_content
    # (100/cycle) would throttle verification far below the ingest rate.
    # refresh_content also tags where the text came from, which is what keeps
    # the ats_text_collapse detector fed with live-ingest data.
    candidates = [p for p in postings if p.active and p.url and p.date_posted >= FALLBACK_CUTOFF_TS]
    # One query to learn which postings already have text, instead of a
    # round trip per posting. The largest source carries ~2,800 active jobs
    # and almost all of them are already cached, so the per-posting form was
    # ~2,800 sequential queries pulling ~15MB to compute a boolean, hourly.
    total = len(candidates)
    have_content = _content_ready_urls([p.url for p in candidates])
    # A posting whose fetch came back empty inside the retry window is not
    # tried again this hour; see FETCH_RETRY_AFTER for why once a day.
    tried_recently = _content_attempted_urls([p.url for p in candidates]) - have_content
    cached = fetch_failed = gone = 0
    for i, p in enumerate(candidates):
        if i % 10 == 0 and _cancelled(task_id):
            logger.info(f"Task {task_id} cancelled mid-ingest")
            return
        if p.url in have_content or p.url in tried_recently:
            continue
        if p.description:
            # The listing call already carried the posting's text, in the
            # same shape the ATS resolver would have fetched it. Storing it
            # is one insert; fetching it again is the request that gets a
            # worker blocked.
            add_ai_result(
                p.url,
                "passed",
                "listing text",
                "content",
                input_content=p.description,
                config_name="content-cache",
            )
            cached += 1
            metrics.INGEST_JOBS.labels(source["name"], "cached").inc()
            continue
        try:
            content, closure = await verdicts.refresh_content(
                p.url, company=p.company, job_title=p.title, context="ingest"
            )
        except Exception as exc:
            fetch_failed += 1
            logger.warning(f"Ingest {source['name']}: content fetch failed for {p.url}")
            # Swallowed on purpose so one page cannot fail the pull, which is
            # exactly why it must be recorded somewhere else.
            telemetry.capture_exception(
                exc, properties={"source": source["name"], "url": p.url, "context": "ingest"}
            )
            continue
        if closure:
            gone += 1
            continue
        if not content:
            fetch_failed += 1
            continue
        cached += 1
        metrics.INGEST_JOBS.labels(source["name"], "cached").inc()
        if cached % 5 == 0:
            _set_progress(task_id, i + 1, total, source["name"])
    # The counts the health detectors read: a feed that returned nothing, a
    # pattern that admits nothing, a worker whose fetches stopped landing.
    # Kept on the task because nothing else records what one ingest saw.
    _set_progress(
        task_id,
        total,
        total,
        source["name"],
        extra={
            "fetched": fetched,
            "kept": len(postings),
            "already_cached": len(have_content),
            "skipped_recent_failure": len(tried_recently),
            "cached": cached,
            "fetch_failed": fetch_failed,
            "gone": gone,
            "retired": retired,
        },
    )

    schedule_filter_runs(payload.get("cycle", "manual"))


def schedule_filter_runs(cycle: str) -> None:
    """One filter run per entitled user per ingest cycle.

    A run that has not split yet (pending or running) blocks the next: two
    splitters would select the same jobs. A run that HAS split and is waiting
    on its chunks does not block, because the splitter excludes every url a
    live chunk holds (board._in_flight_urls) and so judges only what arrived
    since. Before that exclusion the guard covered 'waiting' too, and one
    chunk parked on a straggler batch kept a user's new postings unjudged
    until the provider finished it - 6,226 postings for 14 hours on
    2026-09-04.
    """
    users = db.query(
        """
        SELECT DISTINCT u.id FROM users u
        JOIN user_sources us ON us.user_id = u.id
        JOIN user_filters uf ON uf.user_id = u.id AND uf.enabled
        LEFT JOIN user_settings s ON s.user_id = u.id
        WHERE s.api_key_enc IS NOT NULL
           OR u.groups && ARRAY(SELECT group_name FROM group_budgets)::text[]
        """
    )
    for u in users:
        splitting = db.query_one(
            "SELECT 1 AS x FROM tasks WHERE kind = 'run_all_filters' "
            "AND status IN ('pending', 'running') "
            "AND (payload->>'user_id')::bigint = %s LIMIT 1",
            (u["id"],),
        )
        if splitting:
            continue
        enqueue(
            "run_all_filters",
            {"user_id": u["id"], "batched": True},
            dedupe_key=f"runall:{u['id']}:{cycle}",
        )
