"""Whether every running worker is at least as new as a data step needs.

A data step that changes what a stored row holds can run only once no
running image still reads the old shape. `release` in worker_status is a
commit, which cannot be ordered, so each image also reports LEVEL, a counter
raised by a release whose data step must wait for every older image to
leave. An older image rewrites `release` on every beat but never
`data_level_release`, so a worker counts as at its level only while the two
agree: a host rolled back is below the level again on its next beat.

1: batch_requests rows hold only the member's own fields once the bundle's
   fields are on batch_objects (tasks.batch_objects).
"""

from __future__ import annotations

from api import db

LEVEL = 1

# A worker beats every 60 seconds (api.worker.HEARTBEAT_SECONDS) and the
# fleet screen calls it dead after 90; five minutes waits out a slow beat
# without counting a stopped host as running.
LIVE = "5 minutes"


def behind(level: int) -> int:
    """Running workers whose image is below `level`, or whose level was
    written by a release other than the one they now run."""
    row = db.query_one(
        "SELECT count(*) AS n FROM worker_status "
        f"WHERE last_seen > now() - interval '{LIVE}' "
        "AND (data_level IS NULL OR data_level < %s "
        "OR data_level_release IS DISTINCT FROM release)",
        (level,),
    )
    assert row is not None
    return int(row["n"])
