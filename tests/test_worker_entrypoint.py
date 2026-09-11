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
import queue
import subprocess
import sys
import threading

import pytest

# What the worker logs once main() has run, before it polls for the first
# time. Waiting for the line replaces a fixed eight-second sleep that only
# proved the process had not exited yet. The line is the stronger evidence -
# it says main() ran, which is the thing that regressed - and it arrives in
# about a second.
STARTED_LINE = "Worker started"

# A ceiling, not a wait: it covers a cold import and a schema check on a
# loaded machine, and the test returns the moment the line appears.
STARTUP_TIMEOUT_SECONDS = 60.0


def _lines(stream) -> queue.Queue:
    """The child's output, on a thread, so the test can wait for the line it
    wants with a deadline instead of blocking in readline forever. None marks
    the stream closing, which means the process is exiting."""
    out: queue.Queue = queue.Queue()

    def pump() -> None:
        for line in stream:
            out.put(line)
        out.put(None)

    threading.Thread(target=pump, daemon=True).start()
    return out


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
    # DATABASE_URL, not TEST_DATABASE_URL: under xdist this process holds a
    # database of its own, and the child must write its worker_status row
    # there rather than into the database another worker is resetting.
    env["DATABASE_URL"] = os.environ["DATABASE_URL"]

    proc = subprocess.Popen(
        [sys.executable, "-m", "api.worker"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    seen: list[str] = []
    try:
        lines = _lines(proc.stdout)
        while True:
            try:
                line = lines.get(timeout=STARTUP_TIMEOUT_SECONDS)
            except queue.Empty:
                pytest.fail(
                    f"`python -m api.worker` never logged {STARTED_LINE!r} in "
                    f"{STARTUP_TIMEOUT_SECONDS:.0f}s.\nOutput:\n{''.join(seen)}"
                )
            if line is None:
                # The stream closed, so the process is exiting. exit 0 is the
                # specific regression: a clean fall-through that looks like
                # success. A crash is a different bug, and the message says
                # which one happened.
                exited = proc.wait(timeout=10)
                kind = (
                    "fell through to EOF without running main() - the "
                    "`if __name__ == '__main__'` guard is missing"
                    if exited == 0
                    else f"crashed with exit code {exited}"
                )
                pytest.fail(f"`python -m api.worker` {kind}.\nOutput:\n{''.join(seen)}")
            seen.append(line)
            if STARTED_LINE in line:
                break
        # The line proves main() ran. Still being up proves it went on into
        # the loop rather than dying just after logging.
        assert proc.poll() is None, (
            f"`python -m api.worker` logged {STARTED_LINE!r} and then exited with "
            f"{proc.returncode}.\nOutput:\n{''.join(seen)}"
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)
