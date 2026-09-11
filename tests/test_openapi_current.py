from __future__ import annotations

import json
import pathlib
from typing import Any

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


def test_every_schema_has_a_name_a_generator_can_use():
    """Two routers may not name two different models the same thing.

    FastAPI does not refuse it. It disambiguates, by prefixing the module
    path, so `Source` becomes `api__routers__source_admin__Source` and the
    type a generator emits for the frontend is unusable. The point of
    declaring shapes is that `openapi.json` can be pointed at a generator, so
    a name that survives that round trip is part of the contract, not a
    detail.

    Renaming one of the two is the fix, and the better name is usually the
    more specific one: a catalog row and a person's subscription are both
    "a source" only until you have to hold both.
    """
    from api.app import app

    mangled = sorted(n for n in app.openapi()["components"]["schemas"] if "__" in n)
    # Pydantic names a generic by its parameters, which is long but unique
    # and not a collision.
    mangled = [n for n in mangled if not n.startswith("NonNullUpdate")]
    assert not mangled, (
        "two routers declare these under one name, so FastAPI mangled them:\n  "
        + "\n  ".join(mangled)
    )


def test_no_response_model_declares_a_decimal():
    """A `Decimal` field is served as a JSON string, and nothing says so.

    psycopg returns `numeric` columns as `Decimal`, and for the years these
    routes returned bare dicts FastAPI's encoder turned each one into a
    number. The frontend's hand-written types say `number` and the board
    renders the money fields as numbers.

    Declaring the field as `Decimal` changes that silently: pydantic
    serialises a Decimal to `"100.5"`, a string, and the generated schema says
    `type: string`, so the wire moves and the schema agrees with neither the
    old behaviour nor the client. Found by generating a TypeScript client from
    `openapi.json` and reading what came out.

    Use `float`. It is what was already on the wire, and these are display
    values, not ledger arithmetic: the ledger's own totals are computed in
    Postgres.
    """
    import decimal
    import typing

    from pydantic import BaseModel

    from api.app import app

    def fields_of(model: type[BaseModel], seen: set[type]) -> list[tuple[str, str, Any]]:
        if model in seen:
            return []
        seen.add(model)
        found = []
        for name, field in model.model_fields.items():
            args = typing.get_args(field.annotation) or (field.annotation,)
            for arg in args:
                if arg is decimal.Decimal:
                    found.append((model.__name__, name, arg))
                if isinstance(arg, type) and issubclass(arg, BaseModel):
                    found.extend(fields_of(arg, seen))
            for arg in args:
                for inner in typing.get_args(arg):
                    if isinstance(inner, type) and issubclass(inner, BaseModel):
                        found.extend(fields_of(inner, seen))
        return found

    seen: set[type] = set()
    offenders = []
    for route in app.routes:
        model = getattr(route, "response_model", None)
        if isinstance(model, type) and issubclass(model, BaseModel):
            offenders.extend(fields_of(model, seen))
    assert not offenders, "these serialise as strings; declare them float:\n  " + "\n  ".join(
        f"{m}.{f}" for m, f, _ in offenders
    )
