from __future__ import annotations

from tests.application_support import _available_owner_key as _available_owner_key
from tests.application_support import _job_for, _user_id


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
