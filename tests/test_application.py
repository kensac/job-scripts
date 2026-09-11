"""Answers for the free-response box autofill cannot fill: the form read off
the ATS, the resume and style the drafts are written from, the batched draft
task, and the back-and-forth on one draft."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from api import db
from core import forms
from core.answers import DEFAULT_STYLE
from tasks import application as drafts


@pytest.fixture(autouse=True)
def _available_owner_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-owner-test")


GREENHOUSE = json.dumps(
    {
        "questions": [
            {
                "label": "First Name",
                "required": True,
                "fields": [{"name": "first_name", "type": "input_text"}],
            },
            {
                "label": "Resume/CV",
                "required": True,
                "fields": [
                    {"name": "resume", "type": "input_file"},
                    {"name": "resume_text", "type": "textarea"},
                ],
            },
            {
                "label": "Cover Letter",
                "required": False,
                "fields": [
                    {"name": "cover_letter", "type": "input_file"},
                    {"name": "cover_letter_text", "type": "textarea"},
                ],
            },
            {
                "label": "Why Anthropic?",
                "required": True,
                "fields": [{"name": "question_1001", "type": "textarea"}],
            },
            {
                "label": "LinkedIn Profile",
                "required": False,
                "fields": [{"name": "question_1002", "type": "input_text"}],
            },
            {
                "label": "Are you authorized to work?",
                "required": True,
                "fields": [
                    {"name": "question_1003", "type": "multi_value_single_select", "values": []}
                ],
            },
        ]
    }
)
ASHBY = json.dumps(
    {
        "data": {
            "jobPosting": {
                "applicationForm": {
                    "sections": [
                        {
                            "title": "Contact",
                            "fieldEntries": [
                                {
                                    "field": {
                                        "path": "_systemfield_name",
                                        "title": "Name",
                                        "type": "String",
                                        "isNullable": False,
                                    }
                                },
                                {
                                    "field": {
                                        "path": "_systemfield_resume",
                                        "title": "Resume",
                                        "type": "File",
                                        "isNullable": False,
                                    }
                                },
                                {
                                    "field": {
                                        "path": "b1f2",
                                        "title": "Additional Information",
                                        "type": "LongText",
                                        "isNullable": False,
                                    }
                                },
                            ],
                        }
                    ]
                }
            }
        }
    }
)
LEVER = (
    '<ul><li class="application-question custom-question"><div>'
    '<div class="application-label full-width textarea"><div class="text">Export Control: '
    "list your citizenships<span>✱</span></div></div>"
    '<div class="application-field full-width"><textarea class="card-field-input" '
    'name="cards[abc][field0]"></textarea></div></div></li>'
    '<li class="application-question"><div class="application-label">Full name<span>✱</span></div>'
    '<div class="application-field"><input type="text" name="name"></div></li>'
    '<li class="application-question"><div><div class="application-label full-width textarea">'
    '<div class="text">Additional information</div></div><div class="application-field">'
    '<textarea name="comments"></textarea></div></div></li></ul>'
)
WORKABLE = json.dumps(
    [
        {
            "name": "Personal information",
            "fields": [
                {"id": "firstname", "type": "text", "label": "First name", "required": True}
            ],
        },
        {
            "name": "Profile",
            "fields": [
                {"id": "resume", "type": "file", "label": "Resume", "required": True},
                {
                    "id": "cover_letter",
                    "type": "paragraph",
                    "label": "Cover letter",
                    "required": False,
                },
            ],
        },
        {
            "name": "Questions",
            "fields": [
                {"id": "q9", "type": "paragraph", "label": "Why this role?", "required": True},
                {"id": "q10", "type": "text", "label": "Notice period", "required": False},
            ],
        },
    ]
)


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

        assert forms.budget_host("https://job-boards.greenhouse.io/x/jobs/1") == (
            "boards-api.greenhouse.io"
        )
        assert forms.budget_host("https://boards.greenhouse.io/x/jobs/1") == (
            "boards-api.greenhouse.io"
        )
        assert forms.budget_host("https://jobs.lever.co/x/1") == "jobs.lever.co"
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


class TestResumes:
    def test_pasted_text_is_kept_and_same_name_replaces_it(self, client, user_headers):
        r = client.post(
            "/v1/user/resumes",
            json={"name": "master", "text": "Alice. Python."},
            headers=user_headers,
        )
        assert r.status_code == 201, r.text
        r = client.post(
            "/v1/user/resumes",
            json={"name": "master", "text": "Alice. Rust."},
            headers=user_headers,
        )
        assert r.status_code == 201
        rows = client.get("/v1/user/resumes", headers=user_headers).json()["resumes"]
        assert [(x["name"], x["text"]) for x in rows] == [("master", "Alice. Rust.")]

    def test_a_pdf_is_read_at_upload_and_a_bad_one_is_refused(self, client, user_headers):
        r = client.post(
            "/v1/user/resumes",
            json={"name": "pdf", "pdf_base64": base64.b64encode(b"not a pdf").decode()},
            headers=user_headers,
        )
        assert r.status_code == 400 and r.json()["detail"]["code"] == "INVALID_PDF"
        r = client.post("/v1/user/resumes", json={"name": "empty"}, headers=user_headers)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "NO_TEXT"

    def test_another_person_cannot_see_or_delete_it(self, client, user_headers, other_user_headers):
        rid = client.post(
            "/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers
        ).json()["id"]
        assert client.get("/v1/user/resumes", headers=other_user_headers).json()["resumes"] == []
        assert (
            client.delete(f"/v1/user/resumes/{rid}", headers=other_user_headers).status_code == 404
        )
        assert client.delete(f"/v1/user/resumes/{rid}", headers=user_headers).status_code == 200

    def test_the_writing_style_is_a_setting_and_empty_clears_it(self, client, user_headers):
        r = client.put(
            "/v1/user/settings", json={"writing_style": "Short. Casual."}, headers=user_headers
        )
        assert r.status_code == 200 and r.json()["writing_style"] == "Short. Casual."
        assert (
            client.get("/v1/user/settings", headers=user_headers).json()["writing_style"]
            == "Short. Casual."
        )
        r = client.put("/v1/user/settings", json={"prefs": {}}, headers=user_headers)
        assert r.json()["writing_style"] == "Short. Casual."
        r = client.put("/v1/user/settings", json={"writing_style": ""}, headers=user_headers)
        assert r.json()["writing_style"] is None
        # The built-in default travels with the field it stands in for.
        assert r.json()["default_style"] == DEFAULT_STYLE


def _user_id(sub: str = "test-user") -> int:
    row = db.query_one("SELECT id FROM users WHERE sub = %s", (sub,))
    assert row is not None
    return row["id"]


def _job_for(f, user_id: int, url: str) -> int:
    job_id, _ = f.make_ready_job(url=url, company="Anthropic", title="Engineer")
    f.make_board_row(user_id, job_id)
    return job_id


def _owner_config(monkeypatch):
    from api import ai

    cfg = ai.AIConfig(provider="openai", api_key="k", key_source="owner", model="gpt-5.6-luna")
    monkeypatch.setattr(drafts, "load_config", lambda uid: (SimpleNamespace(), cfg))


@pytest.mark.asyncio
class TestDrafting:
    async def test_the_task_reads_the_form_and_writes_one_draft_per_question(
        self, client, user_headers, f, monkeypatch
    ):
        uid = _user_id()
        job_id = _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/1")
        client.post(
            "/v1/user/resumes",
            json={"name": "master", "text": "Alice. Python."},
            headers=user_headers,
        )
        client.put("/v1/user/settings", json={"writing_style": "Blunt."}, headers=user_headers)
        monkeypatch.setattr(forms, "_get", lambda url: GREENHOUSE)
        _owner_config(monkeypatch)
        submitted = []

        async def fake_run_batched(task_id, shape, specs, *, charged_to_user=False):
            submitted.append((shape.purpose, charged_to_user))
            return [
                f.make_batch_result(
                    task_id,
                    s,
                    text=json.dumps({"answer": f"Because {s.custom_id.partition('|')[2]}."}),
                    error=None,
                    usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
                    model="gpt-5.6-luna",
                )
                for s in specs
                if "Blunt." in s.instructions and "Alice. Python." in s.input
            ], SimpleNamespace(model="gpt-5.6-luna")

        monkeypatch.setattr(drafts, "run_batched", fake_run_batched)
        r = client.post(f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers)
        assert r.status_code == 202, r.text
        task = db.query_one("SELECT id, payload FROM tasks WHERE id = %s", (r.json()["task_id"],))
        await drafts.handle_application_draft(task["id"], task["payload"])

        # Through the standard caller, on its own shape, with the tokens
        # booked to the person rather than the fleet.
        assert submitted == [("application", True)]
        body = client.get(f"/v1/user/jobs/{job_id}/application", headers=user_headers).json()
        assert body["form"]["questions"] == 2 and body["form"]["error"] is None
        by_key = {q["key"]: q for q in body["questions"]}
        assert by_key["question_1001"]["draft"] == "Because question_1001."
        assert by_key["question_1001"]["turns"][0]["role"] == "assistant"
        assert by_key["question_1001"]["turns"][0]["kind"] == "draft"
        assert by_key["question_1001"]["model"] == "gpt-5.6-luna"
        # The one-line box is shown as a field, not drafted unless asked for
        # by key; the view says which is which.
        assert by_key["question_1002"]["draft"] is None
        assert (by_key["question_1001"]["multiline"], by_key["question_1002"]["multiline"]) == (
            True,
            False,
        )
        # Booked to the person, under the task's own purpose.
        usage = db.query_one(
            "SELECT purpose, sum(total_tokens) AS t FROM api_usage WHERE user_id = %s GROUP BY 1",
            (uid,),
        )
        assert (usage["purpose"], usage["t"]) == ("application", 120)

    async def test_a_pasted_question_is_drafted_where_the_form_cannot_be_read(
        self, client, user_headers, f, monkeypatch
    ):
        uid = _user_id()
        job_id = _job_for(f, uid, "https://nvidia.wd5.myworkdayjobs.com/en-US/x/job/y")
        client.post(
            "/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers
        )
        r = client.post(
            f"/v1/user/jobs/{job_id}/application/questions",
            json={"question": "Why NVIDIA?"},
            headers=user_headers,
        )
        assert r.status_code == 201 and r.json()["source"] == "manual"
        key = r.json()["key"]
        # Same text again is the same row.
        assert (
            client.post(
                f"/v1/user/jobs/{job_id}/application/questions",
                json={"question": " Why  NVIDIA? "},
                headers=user_headers,
            ).json()["key"]
            == key
        )
        _owner_config(monkeypatch)

        async def fake_run_batched(task_id, shape, specs, *, charged_to_user=False):
            return [
                f.make_batch_result(
                    task_id, s, text=json.dumps({"answer": "GPUs."}), model="gpt-5.6-luna"
                )
                for s in specs
            ], SimpleNamespace(model="gpt-5.6-luna")

        monkeypatch.setattr(drafts, "run_batched", fake_run_batched)
        r = client.post(f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers)
        task = db.query_one("SELECT id, payload FROM tasks WHERE id = %s", (r.json()["task_id"],))
        await drafts.handle_application_draft(task["id"], task["payload"])
        body = client.get(f"/v1/user/jobs/{job_id}/application", headers=user_headers).json()
        assert body["form"]["supported"] is False
        assert [(q["key"], q["draft"]) for q in body["questions"]] == [(key, "GPUs.")]
        assert (
            client.delete(
                f"/v1/user/jobs/{job_id}/application/questions/{key}", headers=user_headers
            ).status_code
            == 200
        )

    async def test_a_persons_own_key_runs_live_one_question_at_a_time(
        self, client, user_headers, f, monkeypatch
    ):
        from api import ai

        uid = _user_id()
        job_id = _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/2")
        client.post(
            "/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers
        )
        monkeypatch.setattr(forms, "_get", lambda url: GREENHOUSE)
        cfg = ai.AIConfig(provider="openai", api_key="k", key_source="byo", model="gpt-5-mini")
        monkeypatch.setattr(drafts, "load_config", lambda u: (SimpleNamespace(), cfg))

        async def fake_parse(cfg, instructions, input_text, model_cls):
            return model_cls(answer="Live."), {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            }

        monkeypatch.setattr(drafts.ai, "parse", fake_parse)
        r = client.post(f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers)
        task = db.query_one("SELECT id, payload FROM tasks WHERE id = %s", (r.json()["task_id"],))
        await drafts.handle_application_draft(task["id"], task["payload"])
        rows = db.query(
            "SELECT draft, model FROM application_answers WHERE user_id = %s AND job_id = %s",
            (uid, job_id),
        )
        assert [(x["draft"], x["model"]) for x in rows] == [("Live.", "gpt-5-mini")]

    async def test_drafting_needs_a_resume_and_a_visible_job(
        self, client, user_headers, other_user_headers, f
    ):
        uid = _user_id()
        job_id = _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/3")
        r = client.post(f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "NO_RESUME"
        assert (
            client.get(
                f"/v1/user/jobs/{job_id}/application", headers=other_user_headers
            ).status_code
            == 404
        )


class TestRefining:
    def test_one_turn_rewrites_from_the_draft_and_keeps_the_exchange(
        self, client, user_headers, f, monkeypatch
    ):
        from api import ai, budget

        uid = _user_id()
        job_id = _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/4")
        client.post(
            "/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers
        )
        key = client.post(
            f"/v1/user/jobs/{job_id}/application/questions",
            json={"question": "Why us?"},
            headers=user_headers,
        ).json()["key"]
        r = client.put(
            f"/v1/user/jobs/{job_id}/application/answers/{key}",
            json={"draft": "First draft."},
            headers=user_headers,
        )
        assert r.status_code == 200 and r.json()["draft"] == "First draft."
        cfg = ai.AIConfig(provider="openai", api_key="k", key_source="owner", model="gpt-5-nano")
        monkeypatch.setattr(budget, "resolve_ai_config", lambda user_id, ent: cfg)
        seen = {}

        async def fake_parse(cfg, instructions, input_text, model_cls):
            seen["input"] = input_text
            return model_cls(answer="Shorter draft."), {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            }

        monkeypatch.setattr(ai, "parse", fake_parse)
        r = client.post(
            f"/v1/user/jobs/{job_id}/application/answers/{key}/refine",
            json={"instruction": "make it shorter"},
            headers=user_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["draft"] == "Shorter draft."
        assert "Current draft:\nFirst draft." in seen["input"]
        assert "make it shorter" in seen["input"]
        assert [(t["role"], t.get("kind")) for t in r.json()["turns"]] == [
            ("user", "edit"),
            ("user", "instruction"),
            ("assistant", "refine"),
        ]
        # A pasted question is prose by definition.
        assert r.json().get("multiline", True) is True
        # A second turn carries the first request along.
        client.post(
            f"/v1/user/jobs/{job_id}/application/answers/{key}/refine",
            json={"instruction": "mention Python"},
            headers=user_headers,
        )
        assert "Earlier request from the applicant: make it shorter" in seen["input"]

    def test_refining_without_a_key_is_a_402_not_a_500(self, client, user_headers, f, monkeypatch):
        from api import budget

        uid = _user_id()
        job_id = _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/5")
        client.post(
            "/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers
        )
        key = client.post(
            f"/v1/user/jobs/{job_id}/application/questions",
            json={"question": "Why us?"},
            headers=user_headers,
        ).json()["key"]

        def no_key(user_id, ent):
            raise budget.AIAccessError("NO_API_KEY", ent)

        monkeypatch.setattr(budget, "resolve_ai_config", no_key)
        r = client.post(
            f"/v1/user/jobs/{job_id}/application/answers/{key}/refine",
            json={"instruction": "shorter"},
            headers=user_headers,
        )
        assert r.status_code == 402 and r.json()["detail"]["code"] == "NO_API_KEY"


class TestOneDraftAtATime:
    def test_a_second_request_while_the_first_runs_is_refused_and_the_view_shows_it(
        self, client, user_headers, f
    ):
        """The page disabled the button only while it remembered its own
        task id, so a reload could queue the same drafts twice while the
        first parked on the provider's batch."""
        from api import events

        uid = _user_id()
        job_id = _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/6")
        client.post(
            "/v1/user/resumes", json={"name": "master", "text": "Alice."}, headers=user_headers
        )
        r = client.post(f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers)
        assert r.status_code == 202
        task_id = r.json()["task_id"]
        r = client.post(f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers)
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == {
            "code": "IN_PROGRESS",
            "message": "drafts for this job are already being written",
            "task_id": task_id,
        }
        view = client.get(f"/v1/user/jobs/{job_id}/application", headers=user_headers).json()
        assert (view["task"]["id"], view["task"]["status"]) == (task_id, "pending")
        # Parked on the batch is still in flight.
        db.execute("UPDATE tasks SET status = 'awaiting_batch' WHERE id = %s", (task_id,))
        assert (
            client.post(
                f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers
            ).status_code
            == 409
        )
        # Done: the view carries no task and the next request queues.
        db.execute("UPDATE tasks SET status = 'done' WHERE id = %s", (task_id,))
        view = client.get(f"/v1/user/jobs/{job_id}/application", headers=user_headers).json()
        assert view["task"] is None
        assert (
            client.post(
                f"/v1/user/jobs/{job_id}/application/draft", json={}, headers=user_headers
            ).status_code
            == 202
        )
        # The event a page matches on names the job, not only the task.
        sent = []
        monkeypatch_publish = lambda channel, data: sent.append((channel, data))
        original = events._publish
        events._publish = monkeypatch_publish
        events.CENTRIFUGO_API_URL, events.CENTRIFUGO_API_KEY = "http://c", "k"
        try:
            events.publish_task(task_id)
        finally:
            events._publish = original
            events.CENTRIFUGO_API_URL = events.CENTRIFUGO_API_KEY = ""
        channels = {c for c, _ in sent}
        assert channels == {"jobtracker:tasks", f"jobtracker:user.{uid}"}
        assert all(d["task"]["job_id"] == job_id and d["task"]["status"] == "done" for _, d in sent)
