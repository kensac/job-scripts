from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from api import ai, budget, db, metrics, telemetry
from api.ai import AIConfig
from core import catalog, page_fetches, pricing
from core.fetching.hosts import hostname
from core.store import Page, add_ai_result, add_ai_results, ai_result_row

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# THE single way any worker/API path runs an AI check and records its verdict.
# Every row it writes carries the full column set (company, title, model,
# duration, tokens, context) so audit surfaces never show gaps, and metrics
# are incremented in exactly one place. Do not call add_ai_result directly
# from new code paths - route them through here.


async def run_check[T: BaseModel](
    cfg: AIConfig,
    *,
    url: str,
    check_type: str,
    instructions: str,
    input_text: str,
    response_model: type[T],
    verdict_of: Callable[[T], tuple[bool, str | None]],
    booking: budget.Booking,
    page_fetch_id: int | None,
    company: str = "",
    job_title: str = "",
    filter_name: str | None = None,
    prompt_hash: str | None = None,
    context: str = "worker",
) -> tuple[T | None, dict[str, int | None]]:
    """Runs one structured check, records a complete verdict row + metrics.

    verdict_of maps the parsed response to (rejected, reason). Failures are
    recorded (status 'failed') and re-raised for the caller's retry policy.

    The call is booked here, to `booking`, in the transaction that writes its
    verdict, so the verdict names it (model_call_id); a caller does not book
    it again. page_fetch_id is the fetch whose text input_text was built from.
    """
    common: dict[str, Any] = dict(
        url=url,
        check_type=check_type,
        model=cfg.model,
        provider=cfg.provider,
        key_source=cfg.key_source,
        reasoning_effort=cfg.params.get("reasoning_effort") or cfg.params.get("effort"),
        filter_name=filter_name,
        prompt_hash=prompt_hash,
        company=company,
        job_title=job_title,
        instructions=instructions,
        page_fetch_id=page_fetch_id,
        context=context,
        # ai.parse already emits transport metrics for a live call.
        record_call_metrics=False,
    )

    def record(usage: dict[str, int | None], **outcome: Any) -> None:
        duration_ms = int((time.monotonic() - start) * 1000)
        with db.transaction():
            call_id = budget.book_live(booking, cfg.model, usage, duration_ms)
            record_ai_verdict(Verdict(usage=usage, model_call_id=call_id, **common, **outcome))

    start = time.monotonic()
    try:
        parsed, usage = await ai.parse(cfg, instructions, input_text, response_model)
    except Exception as exc:
        record(
            exc.usage if isinstance(exc, ai.PaidParseError) else {},
            rejected=None,
            reason=f"{check_type} check failed: {str(exc)[:100]}",
            parsed_json=None,
            error=str(exc),
        )
        telemetry.capture(
            "ai_call_failed",
            properties={
                "provider": cfg.provider,
                "model": cfg.model,
                "purpose": check_type,
                "context": context,
                "error_class": type(exc).__name__,
                "error": str(exc)[:500],
                "url": url,
            },
        )
        raise
    rejected, reason = (
        verdict_of(parsed) if parsed is not None else (None, "AI returned no parsed response")
    )
    record(
        usage,
        rejected=rejected,
        reason=reason,
        parsed_json=json.dumps(parsed.model_dump()) if parsed is not None else None,
    )
    return parsed, usage


_ZERO_USAGE = dict.fromkeys(
    (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
    ),
    0,
)


@dataclass(frozen=True, kw_only=True)
class Verdict:
    """The shared result shape. A missing decision is a failed attempt.

    Live calls already emit provider metrics inside ai.parse; batch callers
    emit them here. Both paths keep consumed tokens on failed attempts.
    """

    url: str
    check_type: str
    rejected: bool | None
    reason: str | None
    parsed_json: str | None
    usage: dict[str, int | None]
    model: str | None
    provider: str = "openai"
    key_source: str = "owner"
    company: str = ""
    job_title: str = ""
    instructions: str | None = ""
    filter_name: str | None = None
    prompt_hash: str | None = None
    context: str = "worker"
    batched: bool = False
    batch_id: str | None = None
    reasoning_effort: str | None = None
    error: str | None = None
    record_call_metrics: bool = True
    shared_call: bool = False
    request_sha256: str | None = None
    page_fetch_id: int | None = None
    # A live call's ledger row; a batched verdict finds its own on insert
    # (core.store).
    model_call_id: int | None = None

    def __post_init__(self) -> None:
        if self.shared_call:
            if self.usage:
                raise ValueError("a companion verdict cannot carry its own provider usage")
            # The caller already booked this response on another verdict. Explicit
            # zero allocation is different from an absent provider usage receipt.
            object.__setattr__(self, "usage", dict(_ZERO_USAGE))
            object.__setattr__(self, "record_call_metrics", False)

    @property
    def status(self) -> str:
        return "failed" if self.rejected is None else "rejected" if self.rejected else "passed"

    def row(self) -> dict[str, Any]:
        return ai_result_row(
            self.url,
            self.status,
            self.reason,
            self.check_type,
            model=self.model,
            filter_name=self.filter_name,
            prompt_hash=self.prompt_hash,
            company=self.company,
            job_title=self.job_title,
            instructions=self.instructions,
            parsed_json=self.parsed_json,
            config_name=self.context,
            batch_id=self.batch_id,
            reasoning_effort=self.reasoning_effort,
            error=self.error,
            request_sha256=self.request_sha256,
            page_fetch_id=self.page_fetch_id,
            model_call_id=self.model_call_id,
        )

    def count(self) -> None:
        """Process metrics, which a rolled-back transaction cannot take back."""
        metrics.CHECKS.labels(self.check_type, self.status).inc()
        if not self.record_call_metrics:
            return
        usage = self.usage
        metrics.AI_CALLS.labels(
            self.provider, self.model or "unknown", "error" if self.rejected is None else "ok"
        ).inc()
        if not usage:
            return
        cost = pricing.estimate_cost_usd(
            self.model,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            cached_tokens=usage.get("cached_tokens"),
            cache_write_tokens=usage.get("cache_write_tokens"),
            batched=self.batched,
        )
        if cost is not None:
            metrics.AI_COST_USD.labels(self.provider, self.model or "unknown", self.key_source).inc(
                float(cost)
            )


def record_ai_verdict(verdict: Verdict) -> int:
    with db.transaction():
        (query_id,) = add_ai_results([verdict.row()])
    verdict.count()
    return query_id


def record_ai_verdicts(verdicts: list[Verdict]) -> list[int]:
    """record_ai_verdict for many, in one round trip, without metrics.

    The caller counts each verdict after its own transaction commits.
    """
    return add_ai_results([verdict.row() for verdict in verdicts])


def host_paced(url: str) -> bool:
    """True when this url's host has used its hourly fetch allowance.

    app_config fetch_host_limits maps a host to page fetches per hour,
    fleet-wide; a host absent from it is not paced. The allowance is counted
    from the content rows the fleet wrote for that host in the last hour,
    passed or failed, so every worker reads the same ledger and no worker
    needs to know about the others. A host that blocks bursts (www.tesla.com
    served 12 pages in an hour on 2026-09-03 and then blocked 19 of the next
    32) is drip-fed at the rate it tolerates instead of being pulled off.
    """
    limits = db.get_config("fetch_host_limits") or {}
    host = hostname(url)
    per_hour = limits.get(host)
    if not per_hour:
        return False
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM page_fetches WHERE created_at > now() - interval '1 hour' "
        "AND (url LIKE %(https)s OR url LIKE %(http)s)",
        {"https": f"https://{host}/%", "http": f"http://{host}/%"},
    )
    used = row["n"] if row else 0
    if used >= per_hour:
        logger.info(f"{host}: {used} of {per_hour} fetches this hour used; deferring {url}")
        telemetry.capture(
            "fetch_deferred",
            properties={"fetch_host": host, "per_hour": per_hour, "used": used, "url": url},
        )
        return True
    return False


# The run of empty fetches since the url's last successful one. The failed
# content rows refresh_content writes are the only memory of an attempt, so
# the run is read off them rather than kept as state that could disagree.
# It walks each arm's url index, a few dozen rows a url at most.
FETCH_STREAK = """
    SELECT count(*) AS failures, max(f.created_at) AS last_failed
    FROM page_fetches f
    WHERE f.url = {url} AND f.status = 'failed'
      AND f.id > COALESCE((SELECT max(p.id) FROM page_fetches p
                           WHERE p.url = {url} AND p.status = 'passed'), 0)
"""


def fetch_parked_sql(url: str) -> str:
    """A predicate, true when no automatic path may fetch the page at `url`
    (a SQL expression): it is inside the wait after its latest empty fetch,
    which doubles with each consecutive one up to fetch_retry_max_hours, or it
    has failed fetch_give_up_after_failures times running and is unfetchable.
    A manual re-check calls refresh_content without asking, and one success
    ends the run. Thresholds are read on every call, so a change on the
    config page applies on the next cycle; they are validated positive ints,
    which is what makes formatting them in safe."""
    base = int(db.get_config("fetch_retry_after_hours"))
    cap = int(db.get_config("fetch_retry_max_hours"))
    give_up = int(db.get_config("fetch_give_up_after_failures"))
    return f"""EXISTS (
        SELECT 1 FROM ({FETCH_STREAK.format(url=url)}) streak
        WHERE streak.failures >= {give_up}
           OR streak.last_failed > now() - interval '1 hour'
                * LEAST({base} * power(2, streak.failures - 1), {cap}))"""


def fetch_parked_urls(urls: list[str]) -> set[str]:
    """The urls fetch_parked_sql keeps every automatic path away from."""
    if not urls:
        return set()
    rows = db.query(
        f"SELECT u AS url FROM unnest(%s::text[]) u WHERE {fetch_parked_sql('u')}", (urls,)
    )
    return {r["url"] for r in rows}


def fetch_failure_streaks(urls: list[str]) -> dict[str, int]:
    """Consecutive empty fetches since the last success, for urls with any."""
    if not urls:
        return {}
    rows = db.query(
        f"SELECT u AS url, s.failures FROM unnest(%s::text[]) u "
        f"CROSS JOIN LATERAL ({FETCH_STREAK.format(url='u')}) s WHERE s.failures > 0",
        (urls,),
    )
    return {r["url"]: r["failures"] for r in rows}


async def refresh_content(
    url: str,
    company: str = "",
    job_title: str = "",
    context: str = "manual",
    scrape_sem: asyncio.Semaphore | None = None,
) -> tuple[str | None, str | None]:
    """refresh_page, for a caller that keeps no answer pointing at the fetch."""
    page, closure = await refresh_page(url, company, job_title, context, scrape_sem)
    return (page.text if page else None), closure


async def refresh_page(
    url: str,
    company: str = "",
    job_title: str = "",
    context: str = "manual",
    scrape_sem: asyncio.Semaphore | None = None,
) -> tuple[Page | None, str | None]:
    """Re-fetches a posting and returns fresh text, or None when the posting is
    gone. A 'recheck' that reuses cached text can only ever re-run the model
    over the page as it looked before it closed. It cannot discover a closure,
    which is the one thing a recheck is usually asked to do.

    Returns (page, closure_signal): the text and the fetch that stored it.
    closure_signal is 'ats_gone' and only
    that: it is set when the BOARD ITSELF reports the posting deleted, which is
    a fact rather than an inference. A redirect is explicitly not a closure -
    the page comes back and the closed-check judges it.

    scrape_sem, when given, is held ONLY around the browser fetch. ATS
    resolution is cheap and is the common path, so gating it on the scrape
    budget would throttle the fast case behind the slow one."""
    from api import fetching
    from core.fetching import ats

    ats_url = url
    listings_url = None
    if "gh_jid=" in url:
        source = db.query_one(
            "SELECT s.listings_url FROM jobs j JOIN sources s ON s.name = j.source "
            "WHERE j.url = %s",
            (url,),
        )
        ats_url = ats.source_posting_url(url, source["listings_url"] if source else None)
    elif prefixes := ats.eightfold_listing_prefixes(url):
        # An Eightfold tenant is known by a source listing it on this host,
        # never by the posting URL's shape: a guessed endpoint on a host that
        # is not Eightfold's would answer 404 and read as a closure.
        source = db.query_one(
            "SELECT listings_url FROM sources "
            "WHERE starts_with(listings_url, %s) OR starts_with(listings_url, %s) "
            "ORDER BY name LIMIT 1",
            prefixes,
        )
        listings_url = source["listings_url"] if source else None
    ats_res = await (
        asyncio.to_thread(ats.resolve_listed, url, listings_url)
        if listings_url
        else asyncio.to_thread(ats.resolve, ats_url)
    )
    if ats_res.status is ats.Status.GONE:
        record_manual(
            url=url,
            check_type="closed",
            rejected=True,
            reason="ATS reports posting gone",
            company=company,
            job_title=job_title,
            context=context,
        )
        return None, "ats_gone"
    if ats_res.ok and ats_res.text and not fetching.looks_blocked(ats_res.text):
        fetch_id = page_fetches.record(url, "passed", "ats text", ats_res.text)
        if ats_res.posted:
            catalog.fill_date_posted(url, ats_res.posted)
        return Page(fetch_id, ats_res.text), None

    if host_paced(url):
        # Deferred, not failed: no row is written, so the next cycle tries
        # again once the host's hour has room. Writing 'failed' here would
        # park the posting (fetch_parked_sql) and count toward giving up on
        # it, for a host that only asked to be fed slowly.
        return None, None
    # The browserless tier, when the engine config asks for it: a fetch with
    # a real Chrome fingerprint, accepted only when the page plainly came
    # back whole (api.fetching.fetch_static). Anything short of that goes to
    # the browser below, so the tier can save a browser fetch but never
    # replace one with a shell. The row says 'static', so the share each
    # engine serves is readable from the content rows.
    # An Eightfold page fetched without a browser is a shell that clears the
    # gate (see ats.Eightfold), so a posting whose resolver did not answer
    # goes straight to the browser.
    if db.get_config("fetch_engine") == "static_first" and listings_url is None:
        static = await fetching.fetch_static(url, int(db.get_config("static_fetch_min_chars")))
        if static:
            return Page(page_fetches.record(url, "passed", "static", static), static), None
    if scrape_sem is not None:
        async with scrape_sem:
            content, redirected = await fetching.fetch_page(url)
    else:
        content, redirected = await fetching.fetch_page(url)
    if content:
        # A redirect is no longer a verdict. It used to record a closure here
        # without the page being read, which marked 74 live jobs dead - boards
        # rewrite posting URLs constantly and no URL comparison separates a
        # canonicalisation from a bounce. The browser followed the redirect and
        # has the page, so the closed-check reads it and decides. That costs an
        # AI call on redirecting postings and is worth it: the model can tell a
        # posting from a careers index, and a URL cannot.
        if redirected:
            logger.info(f"{url} landed elsewhere; letting the check judge the page")
        return Page(page_fetches.record(url, "passed", "scraped", content), content), None
    else:
        # A fetch that came back with nothing (blocked, timed out, empty) left
        # no record, so every hourly ingest and every backfill tried it again:
        # fulltime had 52 such postings on 2026-09-04 and spent 20 minutes an
        # hour on them, from every worker, which is also most of the fleet's
        # block rate. The row is the memory the callers key off to wait
        # before retrying, and the run of them decides how long and whether
        # to stop (fetch_parked_sql). No input_content, so nothing downstream reads it as
        # a page; extraction_failing counts it, which it could never do before.
        page_fetches.record(url, "failed", "fetch returned nothing")
        telemetry.capture(
            "fetch_failed",
            properties={"url": url, "fetch_host": hostname(url), "context": context},
        )
    return None, None


def record_manual(
    *,
    url: str,
    check_type: str,
    rejected: bool,
    reason: str,
    company: str = "",
    job_title: str = "",
    context: str = "worker",
) -> None:
    """For verdicts decided without an AI call (e.g. ATS says the posting is
    gone) - same complete row shape, same metrics."""
    status = "rejected" if rejected else "passed"
    add_ai_result(
        url,
        status,
        reason,
        check_type,
        company=company,
        job_title=job_title,
        config_name=context,
    )
    metrics.CHECKS.labels(check_type, status).inc()
