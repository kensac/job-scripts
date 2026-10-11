"""Streaming recipe validation retains shape state, never a full value."""

from __future__ import annotations

import io
import tracemalloc

import pytest

from api.apply.recipe_json import InvalidRecipe, validate


def check(raw: bytes) -> None:
    validate(io.BytesIO(raw), "workday")


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
