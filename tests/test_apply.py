"""Assisted apply: the profile, the ladder that fills a form, the bank that
remembers what the person typed, and the report that says what to fix."""

from __future__ import annotations

from api import apply, db
from tests.test_api_jobs import _insert_job, _uid


def test_the_ladder_rungs():
    assert apply.normalize("Phone Number *") == "phone number"
    assert apply.rule_for("Legal Name") == "full_name"
    assert apply.rule_for("First name") == "first_name"
    assert apply.rule_for("Preferred Name (if applicable)") == "preferred_name"
    assert apply.rule_for(
        "Are you authorized to work in the country where the job is located?"
    ) == ("work_authorized")
    assert apply.rule_for("Will you now or in the future require sponsorship?") == (
        "needs_sponsorship"
    )
    assert apply.rule_for("Where did you go to University?") == "school"
    assert apply.rule_for("Why are you interested in Rogo?") is None
    # Phrasings from the 40-form survey.
    assert apply.rule_for("Contact Number") == "phone"
    assert apply.rule_for("Links") == "website"
    assert apply.rule_for("Where have you most recently worked?") == "current_company"
    assert apply.rule_for("Previous Company") == "previous_company"
    assert apply.rule_for("Earliest month you'd be able to join") == "start_date"
    assert apply.rule_for("How did you hear about us?") == "referral_source"
    assert apply.rule_for("How did you discover us?") == "referral_source"
    assert apply.rule_for("What is your current visa status?") == "visa_status"
    assert apply.rule_for("Are you open to relocation?") == "willing_to_relocate"
    assert apply.rule_for("Are you able to work from our SF office 5 days a week?") == (
        "willing_onsite"
    )
    assert apply.rule_for("Are you willing to work on-site in Maryland?") == "willing_onsite"
    assert apply.pick_option("no", ["Yes", "No"]) == "No"
    assert apply.pick_option("yes", ["Yes, I am authorized", "No, I am not"]) == (
        "Yes, I am authorized"
    )
    assert apply.pick_option(apply.DECLINE, ["Male", "Female", "I prefer not to say"]) == (
        "I prefer not to say"
    )
    assert apply.pick_option("Asian", ["Asian (Not Hispanic or Latino)", "White"]) == (
        "Asian (Not Hispanic or Latino)"
    )
    assert apply.pick_option("Purple", ["Yes", "No"]) is None
    # Alternatives in order; whole words, so Asian never lands on Caucasian.
    assert apply.pick_option("South Asian | Asian", ["Asian", "White"]) == "Asian"
    assert apply.pick_option("South Asian | Asian", ["Caucasian", "South Asian"]) == "South Asian"
    assert apply.pick_option("Asian", ["Caucasian", "Black"]) is None
    # No, phrased as a negation.
    assert apply.pick_option(
        "no", ["I am a protected veteran", "I am not a protected veteran"]
    ) == ("I am not a protected veteran")
    assert (
        apply.pick_option(
            "no",
            [
                "Yes, I have a disability",
                "No, I do not have a disability",
                "I do not want to answer",
            ],
        )
        == "No, I do not have a disability"
    )


def _profile() -> dict:
    return {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "+1 555 0100",
        "city": "London",
        "country": "United Kingdom",
        "linkedin": "https://linkedin.com/in/ada",
        "work_authorized": "yes",
        "needs_sponsorship": "no",
        "experience": [{"company": "Analytical Engines", "title": "Engineer", "current": True}],
        "education": [{"school": "Home", "degree": "Bachelor's", "field": "Mathematics"}],
    }


FIELDS = [
    {"key": "_systemfield_name", "label": "Legal Name", "kind": "text", "required": True},
    {"key": "_systemfield_email", "label": "Email", "kind": "text"},
    {"key": "q-phone", "label": "Phone Number", "kind": "text"},
    {
        "key": "q-auth",
        "label": "Are you authorized to work?",
        "kind": "yesno",
        "options": ["Yes", "No"],
    },
    {"key": "q-school", "label": "Where did you go to University?", "kind": "text"},
    {"key": "q-why", "label": "Why are you interested in Rogo?", "kind": "long"},
    {
        "key": "q-office",
        "label": "Are you excited to work in an office?",
        "kind": "yesno",
        "options": ["Yes", "No"],
    },
    {"key": "_systemfield_resume", "label": "Resume", "kind": "file"},
]


def test_a_form_is_filled_from_profile_drafts_and_bank_and_the_bank_learns(client, user_headers):
    uid = _uid(user_headers)
    assert client.put("/v1/user/profile", json=_profile(), headers=user_headers).status_code == 200
    got = client.get("/v1/user/profile", headers=user_headers).json()
    assert got["first_name"] == "Ada" and got["gender"] == apply.DECLINE
    assert got["experience"][0]["company"] == "Analytical Engines"

    job_id = _insert_job("src-ap", "https://jobs.ashbyhq.com/rogo/abc")
    db.execute(
        "INSERT INTO application_answers (user_id, job_id, key, question, draft) "
        "VALUES (%s, %s, 'q-why', 'Why are you interested in Rogo?', 'Because engines.')",
        (uid, job_id),
    )

    body = {"url": "https://jobs.ashbyhq.com/rogo/abc/application?utm=x", "fields": FIELDS}
    first = client.post("/v1/user/apply/resolve", json=body, headers=user_headers).json()
    assert first["job_id"] == job_id
    filled = {f["key"]: (f["rung"], f["value"]) for f in first["fields"]}
    assert filled["_systemfield_name"] == ("profile", "Ada Lovelace")
    assert filled["_systemfield_email"] == ("profile", "ada@example.com")
    assert filled["q-auth"] == ("profile", "Yes")
    assert filled["q-school"] == ("profile", "Home")
    assert filled["q-why"] == ("draft", "Because engines.")
    assert filled["q-office"] == ("", None)
    assert filled["_systemfield_resume"] == ("", None)

    # The person answers the office question, keeps it, and submits.
    done = client.post(
        f"/v1/user/apply/fills/{first['fill_id']}/submitted",
        json={
            "fields": [
                {"key": "q-office", "final": "Yes", "remember": True},
                {"key": "q-school", "final": "Home, remotely"},
            ]
        },
        headers=user_headers,
    ).json()
    assert done["job_id"] == job_id
    row = db.query_one(
        "SELECT status, date_applied FROM user_jobs WHERE user_id = %s AND job_id = %s",
        (uid, job_id),
    )
    assert row and row["status"] == "Application Submitted" and row["date_applied"]

    # The next form with the same label is filled from the bank.
    second = client.post("/v1/user/apply/resolve", json=body, headers=user_headers).json()
    filled = {f["key"]: (f["rung"], f["value"]) for f in second["fields"]}
    assert filled["q-office"] == ("bank", "Yes")
    answers = client.get("/v1/user/answers", headers=user_headers).json()["answers"]
    assert [a["label"] for a in answers] == ["Are you excited to work in an office?"]

    report = client.get("/v1/user/apply/report", headers=user_headers).json()
    assert report["forms_submitted"] == 1 and report["fields"] == len(FIELDS)
    assert report["by_rung"] == {"profile": 5, "draft": 1, "blank": 2}
    assert report["most_often_corrected"] == [
        {"label": "where did you go to university", "count": 1}
    ]
    assert {b["label"] for b in report["most_often_blank"]} == {
        "are you excited to work in an office",
        "resume",
    }


def test_the_profile_and_bank_are_the_persons_own(client, user_headers, other_user_headers):
    client.put("/v1/user/profile", json=_profile(), headers=user_headers)
    assert client.get("/v1/user/profile", headers=other_user_headers).json()["first_name"] == ""
    fill = client.post(
        "/v1/user/apply/resolve",
        json={"url": "https://x.test/apply", "fields": [{"key": "k", "label": "Pronouns"}]},
        headers=user_headers,
    ).json()
    assert fill["job_id"] is None
    client.post(
        f"/v1/user/apply/fills/{fill['fill_id']}/submitted",
        json={"fields": [{"key": "k", "final": "she/her", "remember": True}]},
        headers=user_headers,
    )
    mine = client.get("/v1/user/answers", headers=user_headers).json()["answers"]
    assert mine[0]["value"] == "she/her"
    assert client.get("/v1/user/answers", headers=other_user_headers).json()["answers"] == []
    assert (
        client.delete(f"/v1/user/answers/{mine[0]['id']}", headers=other_user_headers).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/user/apply/fills/{fill['fill_id']}/submitted",
            json={"fields": []},
            headers=other_user_headers,
        ).status_code
        == 404
    )


def test_a_report_keeps_the_page_whole_for_triage(client, user_headers, other_user_headers):
    page = {"title": "Apply", "fields": [{"key": "k", "label": "Odd"}], "html": "<form/>"}
    made = client.post(
        "/v1/user/apply/reports",
        json={"url": "https://x.test/apply", "note": "the select did not take", "page": page},
        headers=user_headers,
    )
    assert made.status_code == 201, made.text
    listed = client.get("/v1/user/apply/reports", headers=user_headers).json()["reports"]
    assert listed[0]["title"] == "Apply" and listed[0]["fields"] == 1
    assert listed[0]["note"] == "the select did not take"
    full = client.get(f"/v1/user/apply/reports/{made.json()['id']}", headers=user_headers).json()
    assert full["page"] == page
    assert client.get("/v1/user/apply/reports", headers=other_user_headers).json()["reports"] == []
    too_big = {"html": "x" * 2_100_000}
    assert (
        client.post(
            "/v1/user/apply/reports",
            json={"url": "https://x.test/apply", "page": too_big},
            headers=user_headers,
        ).status_code
        == 413
    )


def test_resumes_per_person_are_bounded_because_each_carries_its_pdf(client, user_headers):
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('resumes_per_user', '2') "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    )
    for name in ("one", "two"):
        made = client.post(
            "/v1/user/resumes", json={"name": name, "text": "a resume"}, headers=user_headers
        )
        assert made.status_code == 201, made.text
    # Replacing one by name is not a third.
    assert (
        client.post(
            "/v1/user/resumes", json={"name": "two", "text": "newer"}, headers=user_headers
        ).status_code
        == 201
    )
    refused = client.post(
        "/v1/user/resumes", json={"name": "three", "text": "a resume"}, headers=user_headers
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "TOO_MANY_RESUMES"


def test_the_model_fills_what_the_rules_could_not_in_one_call(client, user_headers, monkeypatch):
    """A known fact whose option wording the matcher does not recognise goes
    to the model as a hint with the options; an unknown field goes with
    them in the same call. The answer must be one of the options."""
    from api import ai

    seen = {}

    async def fake_parse(cfg, instructions, input_text, model, timeout=120.0):
        seen["input"] = input_text
        return model(
            answers=[
                {"key": "eth", "answer": "Asian/Pacific Islander"},
                {"key": "office", "answer": "Yes"},
                {"key": "made-up", "answer": "Purple"},
            ]
        ), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(ai, "parse", fake_parse)
    monkeypatch.setattr(
        "api.budget.resolve_ai_config",
        lambda uid, ent: type("Cfg", (), {"model": "m", "key_source": "owner"})(),
    )
    client.put(
        "/v1/user/profile",
        json={**_profile(), "ethnicity": "South Asian | Asian"},
        headers=user_headers,
    )
    eth_options = ["White", "Asian (Not Hispanic or Latino)", "Decline"]
    resolved = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://x.test/apply",
            "fields": [{"key": "eth", "label": "Race", "kind": "select", "options": eth_options}],
        },
        headers=user_headers,
    ).json()["fields"][0]
    # "Asian" is a whole word of that option, so the rule lands it directly.
    assert resolved["rung"] == "profile" and resolved["value"] == "Asian (Not Hispanic or Latino)"

    res = client.post(
        "/v1/user/apply/suggest",
        json={
            "fields": [
                {
                    "key": "eth",
                    "label": "Race",
                    "kind": "select",
                    "options": ["Caucasian", "Asian/Pacific Islander"],
                    "hint": "South Asian | Asian",
                },
                {
                    "key": "office",
                    "label": "Excited about the office?",
                    "kind": "yesno",
                    "options": ["Yes", "No"],
                },
                {
                    "key": "made-up",
                    "label": "Favourite colour",
                    "kind": "select",
                    "options": ["Red", "Blue"],
                },
            ]
        },
        headers=user_headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["answers"] == {"eth": "Asian/Pacific Islander", "office": "Yes"} or (
        res.json()["answers"] == {"office": "Yes"}
    )
    assert "South Asian | Asian" in seen["input"] and "Ada" in seen["input"]


def test_a_yes_no_question_about_a_place_is_not_answered_with_the_place(client, user_headers):
    """ "Are you located within 45 miles of a hub?" names a location, and the
    location rule used to answer it with the city, which no yes/no option
    holds. A yes/no field takes only a yes/no fact; the rest goes to the
    model with the profile. A list of offices does take the city."""
    client.put("/v1/user/profile", json=_profile(), headers=user_headers)
    fields = [
        {
            "key": "hub",
            "label": "Are you located within 45 miles of a hub?",
            "kind": "yesno",
            "options": ["Yes", "No"],
        },
        {
            "key": "office",
            "label": "Preferred Work Location",
            "kind": "select",
            "options": ["SF HQ", "London Office", "Remote"],
        },
        {"key": "where", "label": "Where are you currently located?", "kind": "text"},
        {"key": "prev", "label": "Previous Company", "kind": "text"},
    ]
    got = {
        f["key"]: (f["rung"], f["value"], f.get("hint"))
        for f in client.post(
            "/v1/user/apply/resolve",
            json={"url": "https://x.test/a", "fields": fields},
            headers=user_headers,
        ).json()["fields"]
    }
    assert got["hub"] == ("", None, None)
    # The two questions every posting asks in its own words.
    client.put(
        "/v1/user/profile",
        json={**_profile(), "willing_to_relocate": "yes", "willing_onsite": "yes"},
        headers=user_headers,
    )
    fields2 = [
        {
            "key": "reloc",
            "label": "Are you open to relocation?",
            "kind": "yesno",
            "options": ["Yes", "No"],
        },
        {
            "key": "office",
            "label": "Can you work from our SF office 5 days a week?",
            "kind": "yesno",
            "options": ["Yes", "No"],
        },
        {"key": "explain", "label": "Are you open to relocation? Please explain.", "kind": "text"},
    ]
    got2 = {
        f["key"]: (f["rung"], f["value"])
        for f in client.post(
            "/v1/user/apply/resolve",
            json={"url": "https://x.test/a", "fields": fields2},
            headers=user_headers,
        ).json()["fields"]
    }
    assert got2["reloc"] == ("profile", "Yes") and got2["office"] == ("profile", "Yes")
    assert got2["explain"] == ("", None)
    assert got["office"] == ("profile", "London Office", None)
    assert got["where"] == ("profile", "London, United Kingdom", None)
    assert got["prev"] == ("", None, None)


def test_a_draft_never_names_a_gap_and_an_empty_draft_does_not_fill_a_form(client, user_headers):
    """A draft read "My resume does not include production LLM agents" on a live
    form. The instructions used to ask for exactly that; now they forbid
    naming a gap, and a question with no answer in the resume gets an empty
    draft, which the form resolver treats as no draft at all."""
    from api.tasks import application as drafts

    text = drafts.instructions(None)
    assert "never name a gap" in text and "I have not worked with X" in text
    assert "say so briefly" not in text
    # The wording is a config row, so the next change is an admin edit.
    db.execute(
        "INSERT INTO app_config (key, value) VALUES ('application_draft_instructions', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (db.jsonb("Answer in haiku."),),
    )
    assert drafts.instructions("terse").startswith("Answer in haiku.")
    assert "Writing style" in drafts.instructions("terse")
    db.execute("DELETE FROM app_config WHERE key = 'application_draft_instructions'")
    uid = _uid(user_headers)
    job_id = _insert_job("src-gap", "https://jobs.ashbyhq.com/gap/abc")
    db.execute(
        "INSERT INTO application_answers (user_id, job_id, key, question, draft) "
        "VALUES (%s, %s, 'q', 'Anything else?', '')",
        (uid, job_id),
    )
    got = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://jobs.ashbyhq.com/gap/abc/application",
            "fields": [{"key": "q", "label": "Anything else?", "kind": "long"}],
        },
        headers=user_headers,
    ).json()["fields"][0]
    assert got["rung"] == "" and got["value"] is None


def test_the_form_page_maps_back_to_the_posting_on_every_host():
    from core.forms import posting_urls

    assert posting_urls("https://jobs.ashbyhq.com/rogo/abc/application?utm=x") == [
        "https://jobs.ashbyhq.com/rogo/abc"
    ]
    assert posting_urls("https://jobs.lever.co/shieldai/41c5/apply") == [
        "https://jobs.lever.co/shieldai/41c5"
    ]
    both = [
        "https://job-boards.greenhouse.io/yext/jobs/8174875",
        "https://boards.greenhouse.io/yext/jobs/8174875",
    ]
    assert posting_urls("https://job-boards.greenhouse.io/yext/jobs/8174875#app") == both
    assert (
        posting_urls(
            "https://boards.greenhouse.io/embed/job_app?for=yext&token=8174875&b=https%3A%2F%2Fx"
        )
        == both
    )
    assert posting_urls("https://job-boards.eu.greenhouse.io/acme/jobs/1") == [
        "https://job-boards.eu.greenhouse.io/acme/jobs/1",
        "https://boards.eu.greenhouse.io/acme/jobs/1",
    ]


def test_a_submit_moves_the_board_row_without_a_reload(client, user_headers, monkeypatch):
    """The extension's submit writes a status the way the board's own patch
    does, and an open board hears about it on the person's channel, since a
    status written at submit time is not a task and the task events never
    carried it."""
    from api import events

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(events, "_publish", lambda channel, data: published.append((channel, data)))
    uid = _uid(user_headers)
    job_id = _insert_job("src-rt", "https://jobs.ashbyhq.com/rt/abc")
    fill = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://jobs.ashbyhq.com/rt/abc/application",
            "fields": [{"key": "k", "label": "Name"}],
        },
        headers=user_headers,
    ).json()
    client.post(
        f"/v1/user/apply/fills/{fill['fill_id']}/submitted",
        json={"fields": []},
        headers=user_headers,
    )
    rows = [
        d for c, d in published if c == f"jobtracker:user.{uid}" and d.get("type") == "board_row"
    ]
    assert rows and rows[-1]["job_id"] == job_id and rows[-1]["status"] == "Application Submitted"
    assert rows[-1]["date_applied"] and rows[-1]["hidden"] is False


def test_a_consent_paragraph_is_a_label_too(client, user_headers):
    """A Lever consent card's label is the whole certification paragraph, 600
    characters; the 500 cap answered the extension with a 422 on a live form
    (2026-09-07)."""
    long_label = "I hereby certify that I have not knowingly withheld any information " * 9
    res = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://jobs.lever.co/x/1/apply",
            "fields": [
                {"key": "c", "label": long_label, "kind": "multiselect", "options": ["I agree"]}
            ],
        },
        headers=user_headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["fields"][0]["rung"] == ""
