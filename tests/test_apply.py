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
        "https://jobs.ashbyhq.com/rogo/abc",
        "https://jobs.ashbyhq.com/rogo/abc/",
    ]
    assert posting_urls("https://jobs.lever.co/shieldai/41c5/apply") == [
        "https://jobs.lever.co/shieldai/41c5",
        "https://jobs.lever.co/shieldai/41c5/",
    ]
    # Workable lists its postings with a trailing slash; the form's /apply/
    # comes off and the slash stays a candidate.
    assert posting_urls(
        "https://fis.wd5.myworkdayjobs.com/en-CA/searchjobs/job/US-FL/Engineer_JR03/apply/applyManually"
    ) == [
        "https://fis.wd5.myworkdayjobs.com/en-CA/searchjobs/job/US-FL/Engineer_JR03",
        "https://fis.wd5.myworkdayjobs.com/en-CA/searchjobs/job/US-FL/Engineer_JR03/",
    ]
    assert posting_urls("https://apply.workable.com/eqltech/j/B2593F22F8/apply/") == [
        "https://apply.workable.com/eqltech/j/B2593F22F8",
        "https://apply.workable.com/eqltech/j/B2593F22F8/",
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
    # And a one-box certification is filled: the person asked for every
    # required field to be filled, consents included.
    assert res.json()["fields"][0]["rung"] == "profile"
    assert res.json()["fields"][0]["value"] == "I agree"


def test_consents_are_given_and_a_reader_may_name_the_fact(client, user_headers):
    """The person's standing instruction: every field that has to be filled
    is filled, consents included; the extension relays the consent of the one
    person it fills for. A config-driven reader that knows a selector's fact
    sends it, and the label is not read."""
    client.put(
        "/v1/user/profile", json={**_profile(), "phone": "(814) 441-0134"}, headers=user_headers
    )
    fields = [
        {
            "key": "c1",
            "label": "Applicant Arbitration Agreement Acknowledgement",
            "kind": "multiselect",
            "options": ["I acknowledge that I have read the Arbitration Agreement."],
        },
        {
            "key": "c2",
            "label": "Candidate Confidentiality Acknowledgment",
            "kind": "select",
            "options": ["I agree", "I do not agree"],
        },
        {
            "key": "c3",
            "label": "Do you consent to Socure processing your data?",
            "kind": "yesno",
            "options": ["Yes", "No"],
        },
        {"key": "f1", "label": "some unreadable label", "kind": "text", "fact": "phone_digits"},
        {"key": "f2", "label": "x", "kind": "yesno", "options": ["Yes", "No"], "fact": "yes"},
    ]
    got = {
        f["key"]: (f["rung"], f["value"])
        for f in client.post(
            "/v1/user/apply/resolve",
            json={"url": "https://x.test/a", "fields": fields},
            headers=user_headers,
        ).json()["fields"]
    }
    assert got["c1"] == ("profile", "I acknowledge that I have read the Arbitration Agreement.")
    assert got["c2"] == ("profile", "I agree")
    assert got["c3"] == ("profile", "Yes")
    assert got["f1"] == ("profile", "8144410134")
    assert got["f2"] == ("profile", "Yes")


def test_the_resolve_carries_the_rows_a_group_is_filled_from(client, user_headers):
    """Education and experience on a form are repeated groups; the reader
    fills them one entry at a time from the profile's rows, which ride on
    the resolve response, and reports the group as filled by entry count."""
    client.put("/v1/user/profile", json=_profile(), headers=user_headers)
    res = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://apply.workable.com/acme/j/1/apply/",
            "fields": [
                {
                    "key": "fact:education",
                    "label": "education",
                    "kind": "group",
                    "fact": "education",
                },
                {
                    "key": "fact:experience",
                    "label": "experience",
                    "kind": "group",
                    "fact": "experience",
                },
                {
                    "key": "t",
                    "label": "I identify as transgender (please select one):",
                    "kind": "select",
                    "options": ["Yes", "No", "I don't wish to answer"],
                },
            ],
        },
        headers=user_headers,
    ).json()
    assert res["profile"]["experience"][0]["company"] == "Analytical Engines"
    assert res["profile"]["education"][0]["school"] == "Home"
    got = {f["key"]: (f["rung"], f["value"]) for f in res["fields"]}
    assert got["fact:education"] == ("profile", "1 entries")
    assert got["fact:experience"] == ("profile", "1 entries")
    assert got["t"] == ("profile", "I don't wish to answer")


def test_the_admin_list_keeps_the_model_off_a_field(client, user_headers, monkeypatch):
    """The model never fills what the admin list names (location by default,
    matched as a whole word in label or key); the call goes out without
    those fields and the reply names them, so the panel lists them as the
    person's. A list that covers every field costs no call at all."""
    from api import ai

    seen = {}

    async def fake_parse(cfg, rules, text, schema):
        seen["input"] = text
        return schema(answers=[{"key": "why", "answer": "Because the work is measured."}]), {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }

    monkeypatch.setattr(ai, "parse", fake_parse)
    monkeypatch.setattr(
        "api.budget.resolve_ai_config",
        lambda uid, ent: type("Cfg", (), {"model": "m", "key_source": "owner"})(),
    )
    client.put("/v1/user/profile", json=_profile(), headers=user_headers)
    fields = [
        {"key": "location", "label": "Location (City)", "kind": "select"},
        {
            "key": "q1",
            "label": "Do you reside in one of these locations?",
            "kind": "select",
            "options": ["Yes", "No"],
        },
        {"key": "why", "label": "Why this role?", "kind": "long"},
    ]
    fill_id = client.post(
        "/v1/user/apply/resolve",
        json={"url": "https://x.test/apply", "fields": fields},
        headers=user_headers,
    ).json()["fill_id"]
    res = client.post(
        "/v1/user/apply/suggest",
        json={"fields": fields, "fill_id": fill_id},
        headers=user_headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["skipped"] == ["location"]
    assert res.json()["answers"] == {"why": "Because the work is measured."}
    assert "Location (City)" not in seen["input"] and "reside" in seen["input"]
    # The ledger row says what the model said, submitted or not.
    row = db.query_one("SELECT fields FROM application_fills WHERE id = %s", (fill_id,))
    by_key = {f["key"]: f for f in row["fields"]}
    assert by_key["why"]["rung"] == "ai"
    assert by_key["why"]["ai_answer"] == "Because the work is measured."
    assert by_key["location"]["never_ai"] is True and "ai_answer" not in by_key["q1"]

    seen.clear()
    res = client.post("/v1/user/apply/suggest", json={"fields": fields[:1]}, headers=user_headers)
    assert res.json() == {"answers": {}, "skipped": ["location"], "model": None}
    assert not seen


def test_a_field_the_form_reveals_joins_the_open_fill(client, user_headers):
    """The EEO race question appears once Hispanic/Latino is answered; the
    extension resolves what appeared with the fill's id, and the row grows
    instead of a second row opening. A submitted fill is closed to that."""
    client.put("/v1/user/profile", json=_profile(), headers=user_headers)
    first = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://x.test/apply",
            "fields": [
                {
                    "key": "hispanic_ethnicity",
                    "label": "Are you Hispanic/Latino?",
                    "kind": "select",
                    "options": ["Yes", "No", "Decline To Self Identify"],
                },
            ],
        },
        headers=user_headers,
    ).json()
    second = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://x.test/apply",
            "fill_id": first["fill_id"],
            "fields": [
                {
                    "key": "race",
                    "label": "Please identify your race",
                    "kind": "select",
                    "options": ["Asian", "White"],
                }
            ],
        },
        headers=user_headers,
    ).json()
    assert second["fill_id"] == first["fill_id"]
    assert [f["key"] for f in second["fields"]] == ["race"]
    row = db.query_one("SELECT fields FROM application_fills WHERE id = %s", (first["fill_id"],))
    assert [f["key"] for f in row["fields"]] == ["hispanic_ethnicity", "race"]
    assert (
        db.query_one(
            "SELECT count(*) AS n FROM application_fills WHERE url = 'https://x.test/apply'"
        )["n"]
        == 1
    )

    client.post(
        f"/v1/user/apply/fills/{first['fill_id']}/submitted",
        json={"fields": []},
        headers=user_headers,
    )
    third = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://x.test/apply",
            "fill_id": first["fill_id"],
            "fields": [{"key": "late", "label": "Late", "kind": "text"}],
        },
        headers=user_headers,
    ).json()
    assert third["fill_id"] != first["fill_id"]


def test_the_tables_facts_all_resolve(client, user_headers):
    """The selector table's field names all map to a fact now; each fact has a
    value: the profile's own new fields, the derived phone country, the
    constant no, and a step that carries nothing."""
    assert apply.rule_for("Middle Name") == "middle_name"
    assert apply.rule_for("Address Line 2") == "address_2"
    assert apply.rule_for("Apartment, suite, etc.") == "address_2"
    assert apply.rule_for("Street Address") == "address"
    assert apply.rule_for("Behance URL") == "behance"
    assert apply.rule_for("Phone Type") == "phone_type"
    assert apply.rule_for("Date of Birth") == "birthday"
    assert apply.rule_for("Pronouns (optional)") == "pronouns"
    profile = apply.Profile(**{**_profile(), "middle_name": "Byron", "birthday": "1815-12-10"})
    assert apply.profile_value(profile, "middle_name") == "Byron"
    assert apply.profile_value(profile, "birthday") == "1815-12-10"
    assert apply.profile_value(profile, "phone_type") == "Mobile"
    assert apply.profile_value(profile, "phone_country") == "United Kingdom"
    assert apply.profile_value(profile, "no") == "No"
    assert apply.profile_value(profile, "step") == ""
    assert apply.pick_option(apply.profile_value(profile, "no"), ["Yes", "No"]) == "No"

    assert apply.rule_for("Where you found us") == "referral_source"
    ny = apply.Profile(**{**_profile(), "state": "NY"})
    assert apply.profile_value(ny, "state") == "NY | New York"
    assert (
        apply.pick_option(apply.profile_value(ny, "state"), ["New Jersey", "New York"])
        == "New York"
    )
    assert apply.pick_option(apply.profile_value(ny, "state"), ["NJ", "NY"]) == "NY"

    same = apply.Profile(**{**_profile(), "preferred_name": "Ada"})
    assert apply.profile_value(same, "preferred_name") == ""
    other = apply.Profile(**{**_profile(), "preferred_name": "Countess"})
    assert apply.profile_value(other, "preferred_name") == "Countess"


def test_the_resolve_sends_the_profile_by_decision_not_by_default(client, user_headers):
    """The resolve carries the profile to the person's own extension. A new
    Profile field rides along unless excluded, so this list is the decision:
    a field added to Profile fails here until someone says it may leave the
    server (homelab's note on the d0953bb roll)."""
    client.put("/v1/user/profile", json=_profile(), headers=user_headers)
    res = client.post(
        "/v1/user/apply/resolve",
        json={
            "url": "https://x.test/apply",
            "fields": [{"key": "n", "label": "First Name", "kind": "text"}],
        },
        headers=user_headers,
    ).json()
    sent = set(res["profile"])
    allowed = {
        "first_name",
        "middle_name",
        "last_name",
        "preferred_name",
        "pronouns",
        "email",
        "phone",
        "phone_type",
        "birthday",
        "address",
        "address_2",
        "address_3",
        "city",
        "state",
        "postal_code",
        "country",
        "linkedin",
        "github",
        "website",
        "twitter",
        "behance",
        "dribbble",
        "visa_status",
        "referral_source",
        "work_authorized",
        "needs_sponsorship",
        "willing_to_relocate",
        "willing_onsite",
        "years_experience",
        "desired_salary",
        "start_date",
        "gender",
        "ethnicity",
        "hispanic",
        "veteran",
        "disability",
        "experience",
        "education",
    }
    assert sent == allowed, sorted(sent ^ allowed)
    assert "notes" not in sent and "default_resume_id" not in sent
