"""Shared HTTP client and per-host pacing for `core.fetching`.

`session` handles board pulls, ATS resolvers, form reads, and aggregator feeds
with uniform User-Agent, timeout, and 5xx retries. `stdlib_session` exists for
Workday, whose Cloudflare returns 429 to `requests`. HTTP 429 is never retried
here; callers report it to the host budget (`api.hosts`).
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

# Prefer JSON first in Accept so Oracle returns application/json instead of vnd.oracle.*.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
TIMEOUT = 30.0
# Retry 5xx up to 3 times (1s, 2s, 4s) on idempotent methods; raise_on_status=False
# returns the final 5xx response to the caller. Never retry 429.
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
    """urllib-backed JSON client returning `requests.Response` for Workday.

    Workday's Cloudflare returns 429 to `requests` from fleet IPs while allowing
    stdlib `urllib` (measured 2026-11-20). Retries 5xx like `session`; never retries 429.
    """

    def get(self, url: str, timeout: float = TIMEOUT, **_: Any):
        return self._send("GET", url, None, timeout)

    def post(self, url: str, json: Any = None, timeout: float = TIMEOUT, **_: Any):
        return self._send("POST", url, _json.dumps(json).encode(), timeout)

    def _send(self, method: str, url: str, data: bytes | None, timeout: float):
        """Send one request with 1/2/4s backoff on 5xx; return 429 immediately."""
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


# Shared across concurrent pull threads: `_PACE_SECONDS` is replaced atomically
# and each caller reserves its slot timestamp under `_pace_lock` before sleeping.
_PACE_SECONDS: dict[str, float] = {}
_last_call: dict[str, float] = {}
_pace_lock = threading.Lock()
_on_request: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "on_request", default=None
)


@contextlib.contextmanager
def on_request(watcher: Callable[[str], None]) -> Iterator[None]:
    """Call `watcher` on every request a pull inside this block makes."""
    token = _on_request.set(watcher)
    try:
        yield
    finally:
        _on_request.reset(token)


def set_pace(floors: dict) -> None:
    global _PACE_SECONDS
    _PACE_SECONDS = {str(h): float(s) for h, s in (floors or {}).items() if s}


def pace(url: str, request: str | None = None) -> None:
    """Enforce this host's per-slot pacing floor and notify any active `on_request` watcher."""
    watcher = _on_request.get()
    if watcher is not None:
        watcher(request or url)
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
