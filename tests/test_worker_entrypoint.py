"""The worker actually runs when the container starts it.

This exists because it did not. #142 split worker.py into the tasks/ package
and dropped `if __name__ == "__main__": main()`. The entrypoint is
`python -m api.worker`, so the module imported, defined main(), reached EOF and
exited 0 - a clean exit, which every healthcheck, restart policy and metric
reads as success. The whole worker fleet did nothing for hours while looking
healthy.

Nothing in the rest of the suite can catch this: every other test imports
`main` as a symbol, which succeeds whether or not anything calls it. The only
way to know is to execute the module the way the container does.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

# Long enough that a module which falls through to EOF has certainly exited
# (that takes milliseconds), short enough not to drag the suite. It is not a
# timing guess about the worker: a correctly-guarded worker never exits, so any
# value proves the same thing.
FALLTHROUGH_GRACE_SECONDS = 8.0


def test_module_execution_starts_the_loop():
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        # Claim nothing: this must not race the real suite for queued tasks.
        "JOBTRACKER_WORKER_KINDS": "__entrypoint_probe_never_matches__",
        "JOBTRACKER_INGEST_SCHEDULER": "0",
        "JOBTRACKER_WORKER_POLL": "0.2",
        # start_http_server binds a port for the lifetime of the process; port 0
        # lets the OS pick a free one so a developer running this alongside a
        # real worker does not get an unrelated bind error reported as a
        # failure of the thing under test.
        "JOBTRACKER_METRICS_PORT": "0",
        "JOBTRACKER_WORKER_NAME": "entrypoint-probe",
    }
    db = os.environ.get("TEST_DATABASE_URL")
    if db:
        env["DATABASE_URL"] = db

    proc = subprocess.Popen(
        [sys.executable, "-m", "api.worker"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(FALLTHROUGH_GRACE_SECONDS)
        exited = proc.poll()
        if exited is not None:
            output = proc.communicate()[0]
            # exit 0 is the specific regression: a clean fall-through that
            # looks like success. A crash is a different bug, and the message
            # says which one happened.
            kind = (
                "fell through to EOF without running main() - the "
                "`if __name__ == '__main__'` guard is missing"
                if exited == 0
                else f"crashed with exit code {exited}"
            )
            pytest.fail(f"`python -m api.worker` {kind}.\nOutput:\n{output}")
    finally:
        proc.kill()
        proc.wait(timeout=10)
