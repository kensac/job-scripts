"""Persisted fetching settings and their admin metadata."""

from __future__ import annotations

from pydantic import PositiveInt

from api.config_types import ConfigKey

CONFIG_KEYS: dict[str, ConfigKey] = {
    # How long a posting whose page fetch came back empty waits before any
    # automatic path tries it again, after its first failure. The hourly
    # cycle used to be the retry: 24 attempts a day at the same dead URL from
    # every worker. Each further consecutive failure doubles the wait
    # (verdicts.fetch_parked_sql).
    "fetch_retry_after_hours": ConfigKey(
        section="Fetching",
        default=24,
        value_type=PositiveInt,
        help="Hours a posting whose page fetch came back empty waits before any "
        "automatic path tries it again. Each further consecutive failure doubles the wait.",
    ),
    # The longest the doubling wait grows to. A week: with the 24 h base the
    # retries land on days 1, 3, 7, 14 and 21, so a page that is down for a
    # weekend or blocked for a few days still comes back inside a month.
    "fetch_retry_max_hours": ConfigKey(
        section="Fetching",
        default=168,
        value_type=PositiveInt,
        help="Longest wait, in hours, between automatic retries of a posting whose "
        "page keeps coming back empty.",
    ),
    # Consecutive empty fetches after which no automatic path tries the page
    # again; an admin re-check still does, and a success resets the count.
    # Six spans about three weeks under the two defaults above. Before this
    # the 3,511 postings that had never fetched averaged 9 attempts and
    # reached 50 (production, 2026-10-04), on a flat day each.
    "fetch_give_up_after_failures": ConfigKey(
        section="Fetching",
        default=6,
        value_type=PositiveInt,
        help="Consecutive empty fetches after which a posting is unfetchable: no "
        "automatic path retries it until a manual re-check succeeds.",
    ),
    # The longest a board that keeps failing waits between pulls. After the
    # k-th failure in a row the wait is the board's own interval times
    # 2^(k-1), never less than the interval. Every failure run that ended on
    # its own in the 30 days to 2026-10-04 (502s, 403s) ended within 53 hours,
    # so a three-day cap still pulls a board that came back within three days
    # of its return.
    "ingest_retry_max_hours": ConfigKey(
        section="Fetching",
        default=72,
        value_type=PositiveInt,
        help="Longest wait, in hours, between pulls of a board whose pulls keep failing. "
        "Each failure in a row doubles the board's interval up to this.",
    ),
    # Failed pulls in a row after which the board is switched off, which
    # retires its postings. Eight spans about five days on an hourly board and
    # eighteen on a daily one under the cap above. Before this a failed pull
    # did not count toward the interval: twelve boards, most answering 404,
    # were pulled every hour for up to eleven days, and every board together
    # failed 1,509 pulls in the week to 2026-10-04.
    "ingest_give_up_after_failures": ConfigKey(
        section="Fetching",
        default=8,
        value_type=PositiveInt,
        help="Failed pulls in a row after which a board is switched off and its postings "
        "retired. Switch it back on once its listings URL is fixed; one more failure "
        "switches it off again, and one success ends the run.",
    ),
    # Host -> page fetches per hour, fleet-wide. Empty means no host is paced;
    # a host that blocks bursts (www.tesla.com blocked 19 of 32 in a day
    # after 12 in an hour) is added here, from the alert, at the rate it
    # tolerates. Hosts are data, so none is written into code.
    "fetch_host_limits": ConfigKey(
        section="Fetching",
        default={},
        value_type=dict[str, PositiveInt],
        kind="hosts",
        help="Host to page fetches per hour, fleet-wide. A host that blocks bursts is "
        "drip-fed at this rate instead of being pulled off; the fetch-failure "
        "alert names the host.",
    ),
    # Host -> seconds between LISTING requests per worker process. Workable
    # limits by address and two workers share hetzner's; six seconds was not
    # enough, twenty holds. Read by core.fetching.client through the ingest task.
    # "eightfold.ai" paces every Eightfold tenant's pages together, whatever
    # domain it serves from, because one WAF fronts them: on 2026-10-05 about
    # 110 requests a minute from one address held for ten minutes, and
    # thirteen tenants pulled at once with no pace put that address behind a
    # captcha on every tenant tried but Microsoft. The limit itself was not
    # measured. One second keeps a worker near 60 a minute.
    "ingest_host_pace_seconds": ConfigKey(
        section="Fetching",
        default={"apply.workable.com": 20, "eightfold.ai": 1},
        value_type=dict[str, PositiveInt],
        kind="hosts",
        help="Host to the smallest gap in seconds between pulls from one address, "
        "and between pages inside a pull. The fleet learns the actual gap per "
        "host and address from refusals (GET /admin/host-budgets); this is the "
        "floor it never goes under, not a ceiling.",
    ),
    # Which engine fetches a posting page after the ATS resolvers decline.
    # static_first tries a browserless fetch with a real Chrome fingerprint
    # and falls through to the browser unless the page plainly came back
    # whole; chromium goes straight to the browser as before. Switchable
    # without a roll, so the share each engine serves can be read off the
    # content rows (reason 'static' vs 'scraped') and the choice revisited.
    "fetch_engine": ConfigKey(
        section="Fetching",
        default="static_first",
        value_type=str,
        help="Which engine fetches a posting page after the ATS resolvers decline. "
        "static_first tries a browserless fetch and falls back to the browser "
        "unless the page came back whole; chromium goes straight to the browser.",
        choices=("chromium", "static_first"),
    ),
    # The text-length gate under static_first; see api.fetching.fetch_static
    # for the measurement behind 1,500.
    "static_fetch_min_chars": ConfigKey(
        section="Fetching",
        default=1500,
        value_type=PositiveInt,
        help="Characters of text a browserless fetch must return to be served instead of the browser.",
    ),
    "content_backfill_per_cycle": ConfigKey(
        section="Fetching",
        default=100,
        value_type=PositiveInt,
        help="Never-fetched postings the hourly content backfill fetches per cycle, newest "
        "first. A manual run may pass its own limit.",
    ),
}
