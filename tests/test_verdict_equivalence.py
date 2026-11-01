"""Grouped models' verdicts count as each other's, and only while grouped."""

from __future__ import annotations

import pytest

from core import store
from core.store import add_ai_result
from core.verdict_equivalence import ModelEquivalence
from tasks.verification import reverification
from tasks.verification.questions import _verification_spec, question_sha256

LUNA, HAIKU = "gpt-6-luna", "claude-haiku-5-5"


def _groups(kind: str, *models: str) -> list[dict]:
    return [{"kind": kind, "models": list(models)}]


def test_an_empty_setting_is_keyed_by_model():
    assert ModelEquivalence().answering("custom", LUNA) == [LUNA]


def test_a_group_answers_for_each_member_and_only_its_kind():
    on = ModelEquivalence.model_validate(_groups("custom", LUNA, HAIKU))
    assert on.answering("custom", HAIKU) == on.answering("custom", LUNA) == sorted([LUNA, HAIKU])
    assert on.answering("verify", LUNA) == [LUNA]


@pytest.mark.parametrize(
    "value",
    [
        _groups("custom", LUNA),
        _groups("custom", LUNA, LUNA),
        _groups("custom", LUNA, "no-such-model"),
        _groups("custom", LUNA, HAIKU) + _groups("custom", LUNA, "claude-sonnet-5-5"),
        _groups("filters", LUNA, HAIKU),
        [{"kind": "custom", "models": [LUNA, HAIKU], "note": "x"}],
    ],
)
def test_the_setting_refuses_what_it_cannot_mean(value):
    with pytest.raises(ValueError):
        ModelEquivalence.model_validate(value)


def test_a_filter_verdict_from_a_grouped_model_is_reused_until_ungrouped(set_config):
    from api.verdict_equivalence import answering_models

    url = "https://equiv.test/1"
    add_ai_result(url, "passed", check_type="custom", prompt_hash="h1", model=LUNA)
    assert store.decided_custom_urls([url], "h1", answering_models("custom", HAIKU)) == set()
    set_config("verdict_model_equivalence", _groups("custom", LUNA, HAIKU))
    assert store.decided_custom_urls([url], "h1", answering_models("custom", HAIKU)) == {url}
    set_config("verdict_model_equivalence", [])
    assert store.decided_custom_urls([url], "h1", answering_models("custom", HAIKU)) == set()


def test_reverify_accepts_a_grouped_models_answer_to_the_same_page(f, set_config):
    _, url = f.make_ready_job(source=f.make_source("equiv-src"))
    text = "a long job description " * 40
    asked_of_luna = question_sha256(LUNA, _verification_spec(url, text))
    for check in ("closed", "clearance"):
        add_ai_result(url, "passed", check_type=check, model=LUNA, request_sha256=asked_of_luna)
    jobs = {url: {"company": "C", "title": "T"}}
    assert reverification._reuse_unchanged(HAIKU, [(url, text)], jobs) == set()
    set_config("verdict_model_equivalence", _groups("verify", LUNA, HAIKU))
    assert reverification._reuse_unchanged(HAIKU, [(url, "a changed page " * 40)], jobs) == set()
    assert reverification._reuse_unchanged(HAIKU, [(url, text)], jobs) == {url}
