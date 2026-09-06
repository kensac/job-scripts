"""The pace the fleet keeps against each upstream host, per egress address.

A board host limits by address, not by worker: hetzner and hetzner-2 share
one and refused together while oci was fine. So the budget is a row per
(host, egress_group) that every worker on that address reads and writes.

    take(host)      before a pull: this address's next slot, or when it opens
    refused(host)   on a 429: the gap doubles and the slot moves out
    succeeded(host) on a good pull: the gap narrows toward the configured floor

Additive increase, multiplicative decrease, the loop TCP uses: the pace
settles where the host stops refusing and follows it if the host changes its
mind, with no number to maintain by hand. app_config ingest_host_pace_seconds
is a FLOOR per host, never a ceiling; a host with no entry starts unpaced and
learns its gap from its first refusal.

Every worker in a container carries JOBTRACKER_EGRESS_GROUP naming the
address it speaks from; without it the worker name stands in, which is right
for a host running one worker and wrong for a box running two.
"""

from __future__ import annotations

import datetime
import os
import socket
from urllib.parse import urlparse

from api import db

EGRESS_GROUP = (
    os.environ.get("JOBTRACKER_EGRESS_GROUP")
    or os.environ.get("JOBTRACKER_WORKER_NAME")
    or socket.gethostname()
)

# A refusal opens at least this gap, and no gap grows past the cap: a host
# that refuses at 15 minutes apart is a host to drop, not to wait on.
REFUSED_MIN_SECONDS = 30.0
CAP_SECONDS = 900.0
# How much of the gap a success gives back. 0.9 takes a 30 s gap to under
# 5 s in twenty good pulls and never below the floor.
DECAY = 0.9


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def floor_for(host: str) -> float:
    limits = db.get_config("ingest_host_pace_seconds") or {}
    try:
        return float(limits.get(host) or 0)
    except (TypeError, ValueError):
        return 0.0


def take(host: str, egress: str | None = None) -> datetime.datetime | None:
    """Take this address's next slot for the host. None when taken; otherwise
    the moment the slot opens, for the caller to wait until."""
    egress = egress or EGRESS_GROUP
    floor = floor_for(host)
    db.execute(
        """
        INSERT INTO host_budget (host, egress_group, pace_seconds)
        VALUES (%(h)s, %(e)s, %(floor)s)
        ON CONFLICT (host, egress_group) DO UPDATE
            SET pace_seconds = GREATEST(host_budget.pace_seconds, %(floor)s)
        """,
        {"h": host, "e": egress, "floor": floor},
    )
    taken = db.query_one(
        """
        UPDATE host_budget
           SET next_allowed_at = now() + make_interval(secs => pace_seconds), updated_at = now()
         WHERE host = %(h)s AND egress_group = %(e)s AND next_allowed_at <= now()
        RETURNING next_allowed_at
        """,
        {"h": host, "e": egress},
    )
    if taken:
        return None
    row = db.query_one(
        "SELECT next_allowed_at FROM host_budget WHERE host = %s AND egress_group = %s",
        (host, egress),
    )
    return row["next_allowed_at"] if row else None


def refused(host: str, egress: str | None = None) -> datetime.datetime:
    """The host said too many: double the gap (at least REFUSED_MIN, at most
    CAP) and hold this address off until it has passed."""
    row = db.query_one(
        """
        UPDATE host_budget
           SET pace_seconds = LEAST(%(cap)s, GREATEST(pace_seconds * 2, %(min)s)),
               next_allowed_at = now() + make_interval(
                   secs => LEAST(%(cap)s, GREATEST(pace_seconds * 2, %(min)s))),
               refused = refused + 1, updated_at = now()
         WHERE host = %(h)s AND egress_group = %(e)s
        RETURNING next_allowed_at
        """,
        {"h": host, "e": egress or EGRESS_GROUP, "cap": CAP_SECONDS, "min": REFUSED_MIN_SECONDS},
    )
    if row:
        return row["next_allowed_at"]
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=REFUSED_MIN_SECONDS)


# A refusal count with no success behind it is a block, not a pace: hetzner
# was 0 for 18 against Workable while four other addresses were 15 for 15.
BLOCKED_AFTER = 3
# How long a refused pull waits before any open address may take it.
REQUEUE_SECONDS = 5.0


def soon() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=REQUEUE_SECONDS)


def blocked(ok: int, refused: int) -> bool:
    """Refused repeatedly with nothing ever accepted: the address is shut out
    of the host, and no gap will open it. The slot still reopens at the cap,
    so the address probes once in a while and a lifted block shows as ok > 0."""
    return ok == 0 and refused >= BLOCKED_AFTER


def succeeded(host: str, egress: str | None = None) -> None:
    db.execute(
        """
        UPDATE host_budget
           SET pace_seconds = GREATEST(%(floor)s, pace_seconds * %(decay)s),
               ok = ok + 1, updated_at = now()
         WHERE host = %(h)s AND egress_group = %(e)s
        """,
        {"h": host, "e": egress or EGRESS_GROUP, "floor": floor_for(host), "decay": DECAY},
    )
