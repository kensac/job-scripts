from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api import db
from core.fetching import forms
from tasks import application as drafts
from tests.application_support import GREENHOUSE, _job_for, _owner_config, _user_id
from tests.application_support import _available_owner_key as _available_owner_key
from tests.resume_support import create_text_resume


@pytest.mark.asyncio
class TestDrafting:
    async def test_the_task_reads_the_form_and_writes_one_draft_per_question(
        self, client, user_headers, f, monkeypatch
    ):
        uid = _user_id()
        job_id = _job_for(f, uid, "https://job-boards.greenhouse.io/anthropic/jobs/1")
        create_text_resume(
            client,
            json={"name": "master", "text": "Alice. Python."},
            headers=user_headers,
        )
        client.put("/v1/user/settings", json={"writing_style": "Blunt."}, headers=user_headers)
        monkeypatch.setattr(forms, "_get", lambda url: GREENHOUSE)
        _owner_config(monkeypatch)
        submitted = []

        async def fake_run_batched(task_id, shape, specs, *, payer=None):
            submitted.append((shape.purpose, payer.kind))
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
        assert submitted == [("application", "user")]
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
            "SELECT purpose, sum(total_tokens) AS t FROM model_calls WHERE user_id = %s GROUP BY 1",
            (uid,),
        )
        assert (usage["purpose"], usage["t"]) == ("application", 120)

    async def test_a_pasted_question_is_drafted_where_the_form_cannot_be_read(
        self, client, user_headers, f, monkeypatch
    ):
        uid = _user_id()
        job_id = _job_for(f, uid, "https://nvidia.wd5.myworkdayjobs.com/en-US/x/job/y")
        create_text_resume(client, json={"name": "master", "text": "Alice."}, headers=user_headers)
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

        async def fake_run_batched(task_id, shape, specs, *, payer=None):
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
        create_text_resume(client, json={"name": "master", "text": "Alice."}, headers=user_headers)
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
