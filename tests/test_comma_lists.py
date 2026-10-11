"""A comma list is parsed by `api.params.csv` (query parameters and headers)
or `core.env.env_list` (environment variables), never by a local copy.

Thirteen copies existed, and one had drifted: the admin users lookup accepted
negative ids that the shared user parser refused.
"""

from __future__ import annotations

import re

from api import scoping
from core.env import env_list
from core.paths import PROJECT_ROOT

# sorting.py splits on purpose without dropping blanks: a key and its
# direction are paired by position.
_ALLOWED = {
    "src/api/params.py",
    "src/core/env.py",
    "src/api/sorting.py",
}


def test_no_module_splits_its_own_comma_list():
    split = re.compile(r"""\.split\(\s*['"],['"]""")
    found = {
        str(p.relative_to(PROJECT_ROOT))
        for p in (PROJECT_ROOT / "src").rglob("*.py")
        if split.search(p.read_text())
    }
    assert found <= _ALLOWED, sorted(found - _ALLOWED)


def test_user_ids_keep_only_plain_integers():
    # "²" is a digit to str.isdigit and an error to int(): it used to 500.
    assert scoping.user_ids(" 1, 2,,-3,abc,²,4.0, 5 ") == [1, 2, 5]
    assert scoping.user_ids("") == []
    assert scoping.user_ids(None) == []


def test_env_list_default_applies_only_when_unset(monkeypatch):
    monkeypatch.delenv("X_LIST", raising=False)
    assert env_list("X_LIST", "a,b") == ["a", "b"]
    monkeypatch.setenv("X_LIST", " c ,, d")
    assert env_list("X_LIST", "a,b") == ["c", "d"]
    monkeypatch.setenv("X_LIST", "")
    assert env_list("X_LIST", "a,b") == []
