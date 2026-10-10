"""A run's settings are stored once and found again by their exact content."""

from __future__ import annotations

import pytest

from api import db, run_configs


def test_the_same_settings_intern_to_one_row_and_different_ones_do_not():
    body = {"name": "n", "prompt": "p", "on_ambiguous": "keep", "prompt_hash": "h"}
    first = run_configs.intern(run_configs.FILTER, body)

    assert run_configs.intern(run_configs.FILTER, dict(reversed(body.items()))) == first
    assert run_configs.intern(run_configs.FILTER, {**body, "prompt": "q"}) != first
    assert run_configs.intern(run_configs.BOARD, body) != first
    assert db.query_one("SELECT count(*) AS n FROM run_configs")["n"] == 3


def test_a_row_whose_body_does_not_match_its_digest_is_refused():
    body = {"prompt": "p"}
    config_id = run_configs.intern(run_configs.FILTER, body)
    db.execute('UPDATE run_configs SET body = \'{"prompt": "other"}\' WHERE id = %s', (config_id,))

    with pytest.raises(run_configs.RunConfigUnavailable):
        run_configs.intern(run_configs.FILTER, body)


def test_readers_take_settings_from_config_id():
    flt = {"name": "n", "prompt": "p", "on_ambiguous": "keep", "prompt_hash": "h"}
    assert run_configs.filter_of({"config_id": run_configs.intern(run_configs.FILTER, flt)}) == flt

    config_id = run_configs.intern(run_configs.BOARD, {"prompt": "p", "sources": ["a"]})
    assert run_configs.with_board_settings({"revision": 3, "config_id": config_id}) == {
        "revision": 3,
        "prompt": "p",
        "sources": ["a"],
        "config_id": config_id,
    }


def test_a_config_id_of_the_wrong_kind_is_refused():
    config_id = run_configs.intern(run_configs.BOARD, {"prompt": "p"})
    with pytest.raises(run_configs.RunConfigUnavailable):
        run_configs.filter_of({"config_id": config_id})
