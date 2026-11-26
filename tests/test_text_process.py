"""company_text's batch helpers run in a child process
(core.derivations.text_process)."""

from __future__ import annotations

import pytest

from core.derivations import company_text, text_process

POSTINGS = [
    (
        "Backend Engineer",
        "About Acme Robotics\nWe build robots for warehouses.\nYou will write Go.",
    ),
    ("Data Analyst", "About Acme Robotics\nWe build robots for warehouses.\nYou will write SQL."),
    ("Recruiter", "About Acme Robotics\nWe build robots for warehouses.\nYou will hire people."),
]


def test_the_child_answers_as_the_helpers_do():
    shared_in_process = company_text.shared_paragraphs_of([("Acme Robotics", POSTINGS)])
    texts = [(text, "Acme Robotics", shared_in_process[0][1]) for _, text in POSTINGS]
    with text_process.TextProcess() as helper:
        assert helper.call("shared_paragraphs_of", [("Acme Robotics", POSTINGS)]) == [
            list(answer) for answer in shared_in_process
        ]
        assert helper.call("embedding_texts", texts) == company_text.embedding_texts(texts)
        assert helper._child is not None and helper._child.poll() is None, "answered by the child"


def test_a_child_that_is_gone_leaves_the_call_to_this_process():
    texts = [(POSTINGS[0][1], "Acme Robotics", set())]
    with text_process.TextProcess() as helper:
        child = helper._child
        assert child is not None
        child.kill()
        child.wait()
        assert helper.call("embedding_texts", texts) == company_text.embedding_texts(texts)
        assert helper._child is None


def test_a_helper_that_fails_in_the_child_fails_here_too():
    with text_process.TextProcess() as helper, pytest.raises(ValueError):
        helper.call("embedding_texts", [("only two", "fields")])
