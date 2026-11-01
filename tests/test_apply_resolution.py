"""Apply form resolution, drafts, dynamic fields, and panel context."""

from __future__ import annotations

from api import apply, db
from api.mail import applications
from tests.apply_support import ada_profile
from tests.factories import user_id_for_subject
from tests.job_board_support import _insert_job

ASHBY_FORM_FIELDS = [
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
    uid = user_id_for_subject("test-user")
    assert (
        client.put("/v1/user/profile", json=ada_profile(), headers=user_headers).status_code == 200
    )
    got = client.get("/v1/user/profile", headers=user_headers).json()
    assert got["first_name"] == "Ada" and got["gender"] == apply.DECLINE
    assert got["experience"][0]["company"] == "Analytical Engines"

    job_id = _insert_job("src-ap", "https://jobs.ashbyhq.com/rogo/abc")
    db.execute(
        "INSERT INTO application_answers (user_id, job_id, key, question, draft) "
        "VALUES (%s, %s, 'q-why', 'Why are you interested in Rogo?', 'Because engines.')",
        (uid, job_id),
    )

    body = {
        "url": "https://jobs.ashbyhq.com/rogo/abc/application?utm=x",
        "fields": ASHBY_FORM_FIELDS,
    }
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
        "SELECT status FROM user_jobs WHERE user_id = %s AND job_id = %s",
        (uid, job_id),
    )
    assert row and row["status"] == "Application Submitted"
    assert applications.board_day(uid, job_id)

    # The next form with the same label is filled from the bank.
    second = client.post("/v1/user/apply/resolve", json=body, headers=user_headers).json()
    filled = {f["key"]: (f["rung"], f["value"]) for f in second["fields"]}
    assert filled["q-office"] == ("bank", "Yes")
    answers = client.get("/v1/user/answers", headers=user_headers).json()["answers"]
    assert [a["label"] for a in answers] == ["Are you excited to work in an office?"]

    report = client.get("/v1/user/apply/report", headers=user_headers).json()
    assert report["forms_submitted"] == 1 and report["fields"] == len(ASHBY_FORM_FIELDS)
    assert report["by_rung"] == {"profile": 5, "draft": 1, "blank": 2}
    assert report["most_often_corrected"] == [
        {"label": "where did you go to university", "count": 1}
    ]
    assert {b["label"] for b in report["most_often_blank"]} == {
        "are you excited to work in an office",
        "resume",
    }


def test_a_draft_never_names_a_gap_and_an_empty_draft_does_not_fill_a_form(client, user_headers):
    """A draft read "My resume does not include production LLM agents" on a live
    form. The instructions used to ask for exactly that; now they forbid
    naming a gap, and a question with no answer in the resume gets an empty
    draft, which the form resolver treats as no draft at all."""
    from tasks import application as drafts

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
    uid = user_id_for_subject("test-user")
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
    from core.fetching.forms import posting_urls

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


def test_a_field_the_form_reveals_joins_the_open_fill(client, user_headers):
    """The EEO race question appears once Hispanic/Latino is answered; the
    extension resolves what appeared with the fill's id, and the row grows
    instead of a second row opening. A submitted fill is closed to that."""
    client.put("/v1/user/profile", json=ada_profile(), headers=user_headers)
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


def test_the_panel_can_see_its_three_sources_without_opening_a_fill(client, user_headers):
    uid = user_id_for_subject("test-user")
    assert (
        client.put("/v1/user/profile", json=ada_profile(), headers=user_headers).status_code == 200
    )
    job_id = _insert_job("src-ctx", "https://jobs.ashbyhq.com/rogo/ctx")
    db.execute(
        "INSERT INTO application_answers (user_id, job_id, key, question, draft) "
        "VALUES (%s, %s, 'q-why', 'Why Rogo?', 'Because engines.')",
        (uid, job_id),
    )
    db.execute(
        "INSERT INTO application_answer_bank (user_id, label, label_norm, kind, value) VALUES "
        "(%s, 'Are you excited to work in an office?', "
        "'are you excited to work in an office?', 'yesno', 'Yes')",
        (uid,),
    )

    got = client.get(
        "/v1/user/apply/context",
        params={"url": "https://jobs.ashbyhq.com/rogo/ctx/application?utm=x"},
        headers=user_headers,
    ).json()

    assert got["job"]["id"] == job_id
    assert got["profile"]["first_name"] == "Ada"
    # A blank fact fills nothing, so it is not a row saying the profile has it.
    assert "middle_name" not in got["profile"]
    assert [a["value"] for a in got["answers"]] == ["Yes"]
    assert [(d["key"], d["draft"]) for d in got["drafts"]] == [("q-why", "Because engines.")]

    # The read opens no fill: that is what /resolve is for.
    assert (
        db.query_one("SELECT count(*) AS n FROM application_fills WHERE user_id = %s", (uid,))["n"]
        == 0
    )


def test_a_page_with_no_matching_posting_has_no_drafts(client, user_headers):
    got = client.get(
        "/v1/user/apply/context",
        params={"url": "https://jobs.ashbyhq.com/nobody/none/application"},
        headers=user_headers,
    ).json()
    assert got["job"] is None and got["drafts"] == []
