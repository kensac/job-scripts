from __future__ import annotations

import json
import pathlib

_OPENAPI = pathlib.Path(__file__).resolve().parent.parent / "openapi.json"


def test_committed_openapi_matches_the_routes():
    """`make schema` regenerates this file and CLAUDE.md says to do it when
    routes change. Nothing enforced it, so a stale schema merged green.

    That happened: a rebase reset openapi.json to main's copy, the
    regeneration was never committed, and the branch carried a schema missing
    the very endpoint it added. CI passed, because a generated file nothing
    compares against cannot fail - the shape of check that looks like coverage
    and provides none.

    A wrong schema is worse than a missing one: clients generate against it and
    the error surfaces as a caller sending a parameter the server ignores,
    which is silent on both ends.
    """
    from api.app import app

    generated = json.loads(json.dumps(app.openapi()))
    committed = json.loads(_OPENAPI.read_text())

    if generated == committed:
        return

    gen_paths, com_paths = set(generated["paths"]), set(committed["paths"])
    missing = sorted(gen_paths - com_paths)
    extra = sorted(com_paths - gen_paths)
    changed = sorted(
        p for p in gen_paths & com_paths if generated["paths"][p] != committed["paths"][p]
    )
    raise AssertionError(
        "openapi.json is stale - run `make schema` and commit it.\n"
        f"  routes missing from the file: {missing or 'none'}\n"
        f"  routes in the file that no longer exist: {extra or 'none'}\n"
        f"  routes whose shape changed: {changed or 'none'}"
    )
