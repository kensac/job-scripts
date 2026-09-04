"""Error tracking: raw exceptions and failure events to PostHog.

The division of labour is deliberate. A traceback goes here, where it is
grouped, symbolicated and searchable; a CONDITION (a source failing for a
day, a purpose failing whole, a host blocking) stays in api/health.py, whose
detectors know what the numbers mean. Nothing here decides anything; it
records, in one place, so that a failure inside the service that never
becomes a bad HTTP answer - a worker crash, a retry, a fetch or model call
failing mid-pipeline - is visible somewhere at all.

Off entirely without POSTHOG_API_KEY: every function is a no-op, so tests
and a laptop checkout run without a network destination.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any

from api import metrics

logger = logging.getLogger("jobtracker_telemetry")

SERVICE = "service"
_client: Any = None
HOST = socket.gethostname()
# The commit the image was built from (deploy/Dockerfile sets it from the
# build arg), so a regression is dated to a release rather than to a day.
RELEASE = os.environ.get("JOBTRACKER_REVISION", "unknown")


def init() -> None:
    """Builds the client once, from POSTHOG_API_KEY and POSTHOG_HOST. Called
    at API startup and worker startup; safe to call again."""
    global _client
    if _client is not None:
        return
    key = os.environ.get("POSTHOG_API_KEY")
    # Said once at startup either way. A layer that never raises and is a
    # no-op without a key has two silent modes that look identical from
    # PostHog's side; the log line is what tells "not configured" from
    # "configured and broken", and it is the line a diagnosis starts from.
    if not key:
        logger.warning(f"telemetry: DISABLED (POSTHOG_API_KEY unset); release={RELEASE}")
        return
    from posthog import Posthog

    host = os.environ.get("POSTHOG_HOST", "https://us.posthog.com")
    _client = Posthog(
        project_api_key=key,
        host=host,
        # Long-running processes: the default batching thread is right, and
        # shutdown() flushes what it holds.
        enable_exception_autocapture=False,
    )
    logger.info(f"telemetry: enabled host={host} release={RELEASE} process_host={HOST}")


def shutdown() -> None:
    if _client is not None:
        try:
            _client.shutdown()
        except Exception:
            logger.exception("telemetry: shutdown failed")


def _props(properties: dict[str, Any] | None) -> dict[str, Any]:
    return {"host": HOST, "release": RELEASE, **(properties or {})}


def capture(
    event: str, distinct_id: str = SERVICE, properties: dict[str, Any] | None = None
) -> None:
    """A queryable failure event. Never raises: a telemetry failure must not
    become the second failure of the thing it was recording."""
    if _client is None:
        return
    try:
        _client.capture(distinct_id=distinct_id, event=event, properties=_props(properties))
    except Exception:
        metrics.TELEMETRY_FAILURES.labels("event").inc()
        logger.exception(f"telemetry: capture({event}) failed")


def capture_exception(
    exc: BaseException, distinct_id: str = SERVICE, properties: dict[str, Any] | None = None
) -> None:
    if _client is None:
        return
    try:
        _client.capture_exception(exc, distinct_id=distinct_id, properties=_props(properties))
    except Exception:
        metrics.TELEMETRY_FAILURES.labels("exception").inc()
        logger.exception("telemetry: capture_exception failed")
