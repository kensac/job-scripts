"""Streaming recipe validation retains shape state, never a full value."""

from __future__ import annotations

import tracemalloc

import pytest

from api.apply.recipe_json import InvalidRecipe, RecipeParser


def check(raw: bytes, chunk_size: int = 65536) -> None:
    frames: dict[int, dict] = {}
    state = None
    parser = None
    for start in range(0, len(raw), chunk_size):
        parser = RecipeParser("workday", state, frames.get)
        parser.feed(raw[start : start + chunk_size])
        state = parser.checkpoint()
        if parser.deleted_from is not None:
            frames = {depth: body for depth, body in frames.items() if depth < parser.deleted_from}
        frames.update(parser.changed_frames())
    assert parser is not None, "no input to parse"
    parser.finish()


def test_required_keys_cannot_be_replaced_by_later_duplicates():
    for raw in (
        b'{"name":"Workday","matches":["https://a"],"fields":[],"fields":null}',
        b'{"name":"Workday","matches":["https://a"],"fields":[],"name":"other"}',
        b'{"name":"Workday","matches":["https://a"],"fields":[{"name":"x","variants":[],"variants":null}]}',
    ):
        with pytest.raises(InvalidRecipe, match="duplicate"):
            check(raw)


def test_large_scalar_is_validated_without_becoming_one_python_string():
    raw = (
        b'{"name":"Workday","matches":["https://a"],"fields":[],"extra":"'
        + b"x" * 2_100_000
        + b'"}'
    )
    tracemalloc.start()
    try:
        check(raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < len(raw) // 2


def test_invalid_json_is_rejected_at_its_first_bad_token():
    for raw in (
        b'{"name":"Workday","matches":["https://a"],"fields":[],}',
        b'{"name":"Workday","matches":["https://a"],"fields":[]} trailing',
        b'{"name":"Workday","matches":["https://a"],"fields":[],"x":"\\q"}',
        b'{"name":"Workday","matches":["https://a"],"fields":[],"x":01}',
    ):
        with pytest.raises(InvalidRecipe):
            check(raw)


def test_every_token_can_cross_a_chunk_boundary():
    check(
        b'{"name":"Workday","matches":["https://a"],"fields":[{"name":"x","variants":[]}],'
        b'"extra":"\\u0041\xc3\xa9"}',
        chunk_size=1,
    )


def test_deep_nesting_uses_persisted_frames():
    depth = 2000
    check(
        b'{"name":"Workday","matches":["https://a"],"fields":[],"extra":'
        + b"[" * depth
        + b"0"
        + b"]" * depth
        + b"}",
        chunk_size=64,
    )
