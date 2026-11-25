"""The one HTTP client the fetchers in core.fetching speak through.

Board pulls, ATS resolvers, form reads and the aggregator feeds all send
their requests through `session`, so what a request looks like to a host and
what happens when it fails are decided here, once. A new board format or
resolver builds a request and parses the answer; it does not pick a user
agent, a timeout or a retry. Workday (listing search and posting reads) is
the one exception, sent through the standard library (`stdlib_session`) because its
Cloudflare refuses the requests session. Four copies of those choices had drifted: two
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

import contextlib
import contextvars
import json as _json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict
from urllib3.util.retry import Retry

from core.fetching.hosts import pace_key, slot_key

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
# A 5xx is tried three more times, one, two and four seconds apart, by the
# session's adapter and by stdlib_session alike.
_RETRIES = 3
_RETRY_STATUSES = (500, 502, 503, 504)
_RETRY = Retry(
    total=_RETRIES,
    backoff_factor=1,
    status_forcelist=_RETRY_STATUSES,
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


class _StdlibSession:
    """A JSON request through the standard library, answered as a requests.Response.

    Workday's Cloudflare refuses this module's requests session from the
    fleet's addresses and lets the standard library through. Measured from
    hetzner on 2026-11-20, alternating the two 15 seconds apart: FHLBNY
    answered the session 429 four times out of four and urllib 200 four out
    of four. No Workday pull succeeded from 2026-11-16 to that day. The
    reply keeps the requests shape so the refusal handling in the ingest
    task (a 429 widens the host budget) reads it unchanged. A 5xx is retried
    as the session retries one; a 429 never is. The posting reads (ats.Workday) came through here on
    2026-11-21, when the session got 429 for every tenant from both fleet
    addresses and urllib 200, and half of Workday's postings had gone to the
    browser instead.
    """

    def get(self, url: str, timeout: float = TIMEOUT, **_: Any):
        return self._send("GET", url, None, timeout)

    def post(self, url: str, json: Any = None, timeout: float = TIMEOUT, **_: Any):
        return self._send("POST", url, _json.dumps(json).encode(), timeout)

    def _send(self, method: str, url: str, data: bytes | None, timeout: float):
        """One request, retried on a 5xx as the session's adapter retries one. Without
        it a tenant read through thousands of sliced searches failed whole on
        one server hiccup: Walmart failed every pull from 2026-11-21, on a 502
        and then an empty answer. A 429 is still returned at once."""
        for attempt in range(_RETRIES):
            response = self._once(method, url, data, timeout)
            if response.status_code not in _RETRY_STATUSES:
                return response
            time.sleep(2**attempt)
        return self._once(method, url, data, timeout)

    def _once(self, method: str, url: str, data: bytes | None, timeout: float):
        if not url.startswith("https://"):
            raise ValueError(f"not an https url: {url}")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310 - https only, checked above
            url, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:  # noqa: S310
                status, reason, headers, body = (
                    reply.status,
                    reply.reason,
                    reply.headers,
                    reply.read(),
                )
        except urllib.error.HTTPError as error:
            status, reason, headers, body = error.code, error.reason, error.headers, error.read()
        response = requests.Response()
        response.status_code, response.reason, response.url = status, reason, url
        response.headers = CaseInsensitiveDict(headers.items())
        response._content = body
        response.encoding = "utf-8"
        return response


stdlib_session = _StdlibSession()


# A worker running several pulls at once (api.worker) calls these from
# several threads. The floors are swapped whole, so a reader never sees the
# table half rebuilt, and a call reserves its moment under the lock before
# it sleeps, so two threads asking one host cannot both find it free.
_PACE_SECONDS: dict[str, float] = {}
_last_call: dict[str, float] = {}
_pace_lock = threading.Lock()
# Called once per request a pull makes, before its wait. A pull runs in one
# thread (asyncio.to_thread copies the context), so the caller that sets this
# sees every request of its own pull and none of another's.
_on_request: contextvars.ContextVar[Callable[[], None] | None] = contextvars.ContextVar(
    "on_request", default=None
)


@contextlib.contextmanager
def on_request(watcher: Callable[[], None]) -> Iterator[None]:
    """Call `watcher` on every request a pull inside this block makes."""
    token = _on_request.set(watcher)
    try:
        yield
    finally:
        _on_request.reset(token)


def set_pace(floors: dict) -> None:
    global _PACE_SECONDS
    _PACE_SECONDS = {str(h): float(s) for h, s in (floors or {}).items() if s}


def pace(url: str) -> None:
    """Wait out this host's floor since this process last asked the same slot
    (`hosts.slot_key`), then count the request for whoever is watching the
    pull (`on_request`)."""
    watcher = _on_request.get()
    if watcher is not None:
        watcher()
    wait = _PACE_SECONDS.get(pace_key(url))
    if not wait:
        return
    slot = slot_key(url)
    with _pace_lock:
        at = max(time.monotonic(), _last_call.get(slot, 0.0) + wait)
        _last_call[slot] = at
    ahead = at - time.monotonic()
    if ahead > 0:
        time.sleep(ahead)
