"""A task row is written by the code that owns it, and nowhere else.

The worker claims, requeues and reaps (api/worker.py). The runtime writes
progress, parks, finishes and records batches, all behind the claim
(tasks/runtime/). api/queue.py inserts a task, merges payload keys and cancels.
A handler that restates one of these writes drifts from it: the filter batch
runner's own heartbeat had no claim guard and vouched for the run that replaced
it (#844).
"""

from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

_UPDATE = re.compile(r"\bUPDATE\s+tasks\b", re.IGNORECASE)
_INSERT = re.compile(r"\bINSERT\s+INTO\s+tasks\b", re.IGNORECASE)

_UPDATE_OWNERS = ("tasks/runtime/", "api/worker.py", "api/queue.py")

# Admission paths insert inside a transaction that checked for a conflicting
# task under a lock, and publish only after that transaction commits, which
# queue.enqueue (it publishes immediately) cannot do for them.
_INSERT_OWNERS = {
    "api/queue.py": "enqueue",
    "api/task_admission.py": "drafts, uploads and source pulls, under the subject lock",
    "api/filter_runs.py": "filter runs, under the user lock",
    "api/managed_board_runs.py": "managed-board runs, after the budget reservation",
    "api/routers/admin/job_profiles.py": "one classify run, after locking any active one",
}


def _writers(pattern: re.Pattern[str]) -> set[str]:
    return {
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py") if pattern.search(path.read_text())
    }


def test_only_the_runtime_worker_and_queue_update_a_task():
    strays = {p for p in _writers(_UPDATE) if not p.startswith(_UPDATE_OWNERS)}
    assert not strays, (
        f"{sorted(strays)} update tasks directly. Use tasks.runtime (set_progress, "
        "checkpoint, park_waiting, finish) or api.queue (merge_payload, cancel)."
    )


def test_only_admission_paths_insert_a_task():
    strays = _writers(_INSERT) - set(_INSERT_OWNERS)
    assert not strays, f"{sorted(strays)} insert tasks directly. Use api.queue.enqueue."
