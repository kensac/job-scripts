"""A sponsor's weekly allowance is shared by all of their boards, so two
admissions for different boards of one sponsor must not both spend it."""

from __future__ import annotations

import contextlib
import threading

import pytest

from api import db, managed_boards
from api.managed_boards import admission
from core.payloads.objects import PayloadStore
from core.pool import in_transaction
from tests.factories import ObjectClient
from tests.test_managed_board_execution import _admissible

RESERVED = 600


@pytest.fixture
def objects(monkeypatch):
    store = PayloadStore(ObjectClient(), "test-payloads")
    monkeypatch.setattr(PayloadStore, "from_env", lambda: store)
    return store


def _board(sponsor: int, slug: str) -> int:
    return db.query_one(
        "INSERT INTO managed_boards (slug, name, sponsor_user_id, prompt, prompt_hash, "
        "requested_model) VALUES (%s, %s, %s, 'criterion', 'hash', 'gpt-6-luna') RETURNING id",
        (slug, slug, sponsor),
    )["id"]


def test_two_boards_of_one_sponsor_cannot_both_spend_one_allowance(f, monkeypatch, objects):
    _admissible(monkeypatch)
    # Room for one run's reservation, not two.
    monkeypatch.setattr(admission.budget, "owner_budget", lambda groups: (True, RESERVED * 3 // 2))
    monkeypatch.setattr(admission, "_reservation", lambda board, candidates: RESERVED)
    sponsor = f.make_user(sub="shared-sponsor")
    boards = [_board(sponsor, "one"), _board(sponsor, "two")]

    # Each admission waits after its locked recheck until the other has made
    # its own, so without a lock both share, both pass before either inserts.
    # With one, the second waits on the lock, the first's wait times out, and
    # the second rechecks after the first commits.
    checked = threading.Barrier(2, timeout=5)
    recheck = admission._refuse_over_budget

    def recheck_then_wait(sponsor_id, cap, reserved):
        recheck(sponsor_id, cap, reserved)
        if in_transaction():
            with contextlib.suppress(threading.BrokenBarrierError):
                checked.wait()

    monkeypatch.setattr(admission, "_refuse_over_budget", recheck_then_wait)
    outcomes: list[str] = []

    def admit(board_id: int) -> None:
        try:
            managed_boards.admit(board_id)
            outcomes.append("queued")
        except managed_boards.RunRefusal as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=admit, args=(board_id,)) for board_id in boards]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert sorted(outcomes) == sorted(["queued", admission.budget.BUDGET_EXCEEDED])
    assert db.query_one("SELECT count(*) AS n FROM tasks")["n"] == 1
