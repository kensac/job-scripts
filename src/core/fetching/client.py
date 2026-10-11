"""The one HTTP client the fetchers in core.fetching speak through.

Board pulls, ATS resolvers, form reads and the aggregator feeds all send
their requests through `session`, so what a request looks like to a host and
what happens when it fails are decided here, once. A new board format or
resolver builds a request and parses the answer; it does not pick a user
agent, a timeout or a retry. Four copies of those choices had drifted: two
user agents, timeouts of 10, 20 and 30 seconds, and retry in only the board
pulls and a hand-rolled backoff in the aggregator feeds.

The host budget (api.hosts) is what answers a refusal: a 429 is returned to
the caller, never slept on and retried here, because the budget widens the
gap for every worker on the address, and a retry inside one request would
spend the refused address again before the budget saw the refusal.

`pace` is the gap between requests to one host from this process, inside a
pull. Its floors are ingest_host_pace_seconds, which the ingest task hands
in before each pull because core cannot read app_config, keyed by
hosts.pace_key like the budget row.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.fetching.hosts import pace_key

# The resolvers sent this user agent with "*/*" and the board pulls sent
# "Mozilla/5.0" with JSON first. The first request to three active sources of
# every board format answered the same status and length under each on
# 2026-10-10; Oracle labels its JSON application/json only when asked for
# JSON (vnd.oracle.adf.resourcecollection+json to "*/*"), so JSON is asked
# for first and anything else still accepted.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
# Connect and read, per request. The slowest page measured is ByteDance's
# 500-row reply at about 11 seconds (2026-10-05); the board pulls had 30, the
# resolvers and form reads 20 and the aggregator feed 10, none of them
# measured. One bound that clears the slowest known page.
TIMEOUT = 30.0
# The hourly cycle is the real retry; this only rides out a blip inside one
# fetch, the three retries the board pulls and aggregator feeds already made.
# Only idempotent methods, which is urllib3's default: a POST search is not
# replayed. raise_on_status=False hands the last 5xx back as a response, so
# its status reaches the caller instead of a RetryError without one.
_RETRY = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=(500, 502, 503, 504),
    respect_retry_after_header=False,
    raise_on_status=False,
)


class _Session(requests.Session):
    def request(self, method: str, url: str, *args: Any, **kwargs: Any):
        kwargs.setdefault("timeout", TIMEOUT)
        return super().request(method, url, *args, **kwargs)


session = _Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"})
for _prefix in ("https://", "http://"):
    session.mount(_prefix, HTTPAdapter(max_retries=_RETRY))


# A worker running several pulls at once (api.worker) calls these from
# several threads. The floors are swapped whole, so a reader never sees the
# table half rebuilt, and a call reserves its moment under the lock before
# it sleeps, so two threads asking one host cannot both find it free.
_PACE_SECONDS: dict[str, float] = {}
_last_call: dict[str, float] = {}
_pace_lock = threading.Lock()


def set_pace(floors: dict) -> None:
    global _PACE_SECONDS
    _PACE_SECONDS = {str(h): float(s) for h, s in (floors or {}).items() if s}


def pace(url: str) -> None:
    """Wait out this host's floor since this process last asked it."""
    host = pace_key(url)
    wait = _PACE_SECONDS.get(host)
    if not wait:
        return
    with _pace_lock:
        at = max(time.monotonic(), _last_call.get(host, 0.0) + wait)
        _last_call[host] = at
    ahead = at - time.monotonic()
    if ahead > 0:
        time.sleep(ahead)
