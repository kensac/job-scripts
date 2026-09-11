"""Every failure this application raises is in the contract.

The schema carried 200, 201, 202 and the 422 FastAPI adds for validation, and
nothing else, while 164 sites raised an `HTTPException`. So a client could not
know what a 400 or a 404 held and guessed from a response it had seen, and the
guess was per client.

`api/problem.py` fixed the SHAPE. This fixes the coverage: a status raised in
`src/` and declared nowhere is a failure the contract does not mention.
"""

from __future__ import annotations

import json
import pathlib
import re

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
_OPENAPI = pathlib.Path(__file__).resolve().parent.parent / "openapi.json"

# A 500 is a bug, not a refusal. It has no code a client can act on and
# declaring one would invite a client to handle it as an outcome.
_NOT_A_CONTRACT = {500}

_RAISED = re.compile(r"(?:refuse\(\s*|HTTPException\(\s*|status_code=)(\d{3})")


def _statuses_raised() -> set[int]:
    found: set[int] = set()
    for path in _SRC.rglob("*.py"):
        found |= {int(m) for m in _RAISED.findall(path.read_text())}
    return {s for s in found if s >= 400} - _NOT_A_CONTRACT


def _statuses_declared() -> set[int]:
    spec = json.loads(_OPENAPI.read_text())
    declared: set[int] = set()
    for operations in spec["paths"].values():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            declared |= {
                int(code)
                for code in operation.get("responses", {})
                if code.isdigit() and int(code) >= 400
            }
    return declared


def test_every_status_the_code_raises_is_in_the_schema():
    """Not per route, which no static read can know, but per application: a
    status nothing declares anywhere is one nothing documented at all.

    `api/problem.py` has a named set per reason, so declaring one is attaching
    the set the route's failure belongs to rather than writing the dict again.
    """
    undeclared = sorted(_statuses_raised() - _statuses_declared())
    assert not undeclared, (
        "raised in src/ and declared nowhere in the schema: "
        f"{undeclared}\nattach the matching set from api.problem to the route that raises it"
    )


def test_no_refusal_set_declares_a_status_nothing_raises():
    """The other direction, because an over-declared failure is a client
    handling a case that cannot happen."""
    from api import problem

    named = set()
    for value in vars(problem).values():
        if isinstance(value, dict) and all(isinstance(k, int) for k in value):
            named |= set(value)
    unraised = sorted(named - _statuses_raised())
    assert not unraised, f"api.problem declares {unraised}, which nothing in src/ raises"
