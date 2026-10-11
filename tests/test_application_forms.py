"""Application questions and drafts, grouped by the behavior each test proves."""

from __future__ import annotations

import pytest

from core.fetching import forms
from tests.application_support import ASHBY, GREENHOUSE, LEVER, WORKABLE
from tests.application_support import _available_owner_key as _available_owner_key


class TestReadingTheForm:
    """Four hosts publish the form; the fields autofill already handles are
    left out, and only the employer's own questions come back."""

    def test_greenhouse_keeps_the_employers_questions_only(self, monkeypatch):
        monkeypatch.setattr(forms, "_get", lambda url: GREENHOUSE)
        qs = forms.fetch("https://job-boards.greenhouse.io/anthropic/jobs/5343907008")
        assert [(q.key, q.kind, q.required) for q in qs] == [
            ("question_1001", "long", True),
            ("question_1002", "short", False),
        ]
        assert qs[0].label == "Why Anthropic?"

    def test_ashby_reads_the_long_text_fields(self, monkeypatch):
        monkeypatch.setattr(forms, "_post_json", lambda url, body: ASHBY)
        qs = forms.fetch("https://jobs.ashbyhq.com/openai/0f887fe6-39f4-44f6-8ee5-230c3002f0d7")
        assert [(q.key, q.label, q.required) for q in qs] == [
            ("b1f2", "Additional Information", True)
        ]

    def test_lever_reads_the_apply_page(self, monkeypatch):
        monkeypatch.setattr(forms, "_get", lambda url: LEVER)
        qs = forms.fetch("https://jobs.lever.co/zoox/18b6dfa0-d581-4d29-ac79-5d6e666aa9c3")
        assert [(q.key, q.label, q.required) for q in qs] == [
            ("cards[abc][field0]", "Export Control: list your citizenships", True)
        ]

    def test_workable_reads_the_form_by_shortcode(self, monkeypatch):
        seen = []
        monkeypatch.setattr(forms, "_get", lambda url: seen.append(url) or WORKABLE)
        qs = forms.fetch("https://apply.workable.com/eqltech/j/0E026F8FD6/")
        assert seen == ["https://apply.workable.com/api/v1/jobs/0E026F8FD6/form"]
        assert [(q.key, q.kind) for q in qs] == [("q9", "long"), ("q10", "short")]

    def test_a_form_read_is_paced_under_the_host_it_speaks_to(self, monkeypatch):
        """Greenhouse's form is read from boards-api.greenhouse.io, the host
        the listing pulls already pace under; keyed by the posting host the
        reads had their own row and ignored a Greenhouse backoff."""
        from api import db, hosts
        from tasks import application as drafts

        monkeypatch.setattr(forms, "_get", lambda url: GREENHOUSE)
        db.execute(
            "INSERT INTO host_budget (host, egress_group, pace_seconds, next_allowed_at) "
            "VALUES ('boards-api.greenhouse.io', %s, 60, now() + interval '1 minute')",
            (hosts.EGRESS_GROUP,),
        )
        with pytest.raises(drafts.Deferred):
            drafts.read_form("https://job-boards.greenhouse.io/x/jobs/1")

    def test_a_host_this_code_cannot_read_says_so(self):
        assert forms.fetch("https://nvidia.wd5.myworkdayjobs.com/en-US/x/job/y") is None
        assert forms.fetch("https://boards.greenhouse.io/embed/job_app?token=1") is None
        assert not forms.supported("https://jobs.smartrecruiters.com/Canva/1")
