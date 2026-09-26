from api import db
from api.apply import drafting


def test_shared_motivation_guidance_survives_saved_resume_only_instructions():
    db.execute(
        "UPDATE app_config SET value = %s WHERE key = 'application_draft_instructions'",
        (db.jsonb("Write only about what the resume shows."),),
    )
    rules = drafting.instructions("Short and conversational.")
    assert "Motivation questions" in rules
    assert "company-specific reason" in rules
    assert "Short and conversational." in rules
    assert "never invent" in rules.lower()


def test_extension_suggestions_receive_posting_and_personal_style(
    client, user_headers, f, monkeypatch
):
    from api import ai

    job_id, _ = f.make_ready_job(
        url="https://example.test/motivation", company="Example", title="Engineer"
    )
    client.put(
        "/v1/user/settings", headers=user_headers, json={"writing_style": "Short and direct."}
    )
    captured = []

    async def parse(cfg, rules, text, schema):
        captured.append((rules, text))
        return schema(answers=[]), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(ai, "parse", parse)
    monkeypatch.setattr(
        "api.budget.resolve_ai_config",
        lambda uid, ent: type("Cfg", (), {"model": "m", "key_source": "owner"})(),
    )
    # Seed through the same raw-content cache read by the application drafts.
    db.execute(
        "INSERT INTO ai_queries (url, check_type, status, input_content) "
        "VALUES (%s, 'extract', 'passed', %s)",
        ("https://example.test/motivation", "Build reliable clinic scheduling software."),
    )
    response = client.post(
        "/v1/user/apply/suggest",
        headers=user_headers,
        json={
            "job_id": job_id,
            "fields": [{"key": "why", "label": "Why Example?", "kind": "long"}],
        },
    )
    assert response.status_code == 200, response.text
    assert len(captured) == 1
    rules, text = captured[0]
    assert "Motivation questions" in rules
    assert "Short and direct." in rules
    assert "Build reliable clinic scheduling software." in text


def test_missing_company_evidence_is_explicit():
    text = drafting.question_input("Why us?", "Example", "Engineer", "", "Built APIs.")
    assert "no posting text captured" in text
    rules = drafting.instructions(None)
    assert "Do not invent company details" in rules
    assert "empty string" in rules


def test_external_application_supplies_posting_without_creating_catalog_job(
    client, user_headers, monkeypatch
):
    from api import ai
    from core.fetching import ats

    url = "https://jobs.ashbyhq.com/ivo-inc/b31e7195-37dd-4631-8648-422cecbb3f83/application?utm_source=Otta"
    fields = [{"key": "why", "label": "Why Ivo?", "kind": "long", "options": []}]
    fill = client.post(
        "/v1/user/apply/resolve",
        headers=user_headers,
        json={"url": url, "host": "ashby", "fields": fields},
    )
    assert fill.status_code == 200, fill.text
    assert fill.json()["job_id"] is None
    captured = []
    fetched = []

    def resolve(target):
        fetched.append(target)
        return ats.AtsResult(
            ats.Status.OK,
            "Software Engineer at Ivo. Build tools for legal contract review.",
            "ashby",
        )

    async def parse(cfg, rules, text, schema):
        captured.append(text)
        return schema(
            answers=[{"key": "why", "answer": "I want to build useful tools for legal teams."}]
        ), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(ats, "resolve", resolve)
    monkeypatch.setattr(ai, "parse", parse)
    monkeypatch.setattr(
        "api.budget.resolve_ai_config",
        lambda uid, ent: type("Cfg", (), {"model": "m", "key_source": "owner"})(),
    )
    response = client.post(
        "/v1/user/apply/suggest",
        headers=user_headers,
        json={"fill_id": fill.json()["fill_id"], "fields": fields},
    )
    assert response.status_code == 200, response.text
    assert "Build tools for legal contract review." in captured[0]
    assert fetched == [url.split("/application")[0]]
    assert db.query_one("SELECT count(*) AS n FROM jobs")["n"] == 0
