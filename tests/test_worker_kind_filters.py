"""A host claims what both kind filters agree on.

The allowlist fails closed: a kind added later is silently never claimed by a
host pinned to one, which is why kanishk-desktop was capped with concurrency
limits instead of an allowlist. The denylist fails open, so it is the right
shape for a host limited by hardware - a 1GB free-tier VM that cannot run
chromium excludes the browser kinds and still picks up whatever is added next.
"""

from api.worker import _env_list, _kinds_clause

ALLOW = "AND kind = ANY(%(kinds)s)"
DENY = " AND NOT (kind = ANY(%(exclude)s))"


def test_neither_set_claims_everything():
    assert _kinds_clause([], []) == ""


def test_allowlist_only_restricts():
    assert _kinds_clause(["classify_mail", "poll_batches"], []) == ALLOW


def test_denylist_only_subtracts():
    assert _kinds_clause([], ["ingest_source", "run_filter"]) == DENY


def test_both_set_is_an_intersection_not_a_precedence_fight():
    assert _kinds_clause(["a", "b", "c"], ["b"]) == ALLOW + DENY


def test_env_list_drops_whitespace_and_empties(monkeypatch):
    monkeypatch.setenv("X", " a , , b ")
    assert _env_list("X") == ["a", "b"]


def test_env_list_unset_is_empty(monkeypatch):
    monkeypatch.delenv("Y", raising=False)
    assert _env_list("Y") == []
