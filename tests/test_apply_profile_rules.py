"""Apply profile ownership, field rules, and consent facts."""

from __future__ import annotations

from api import apply
from tests.apply_support import ada_profile


def test_the_ladder_rungs():
    assert apply.normalize("Phone Number *") == "phone number"
    assert apply.rule_for("Legal Name") == "full_name"
    assert apply.rule_for("First name") == "first_name"
    for label in ("Preferred First & Last Name", "First and last name", "First name / Last name"):
        assert apply.rule_for(label) == "full_name"
    assert apply.rule_for("Preferred Last Name") == "last_name"
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


def test_the_profile_and_bank_are_the_persons_own(client, user_headers, other_user_headers):
    client.put("/v1/user/profile", json=ada_profile(), headers=user_headers)
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


def test_a_yes_no_question_about_a_place_is_not_answered_with_the_place(client, user_headers):
    """ "Are you located within 45 miles of a hub?" names a location, and the
    location rule used to answer it with the city, which no yes/no option
    holds. A yes/no field takes only a yes/no fact; the rest goes to the
    model with the profile. A list of offices does take the city."""
    client.put("/v1/user/profile", json=ada_profile(), headers=user_headers)
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
        json={**ada_profile(), "willing_to_relocate": "yes", "willing_onsite": "yes"},
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
        "/v1/user/profile", json={**ada_profile(), "phone": "(814) 441-0134"}, headers=user_headers
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
    client.put("/v1/user/profile", json=ada_profile(), headers=user_headers)
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
    profile = apply.Profile(**{**ada_profile(), "middle_name": "Byron", "birthday": "1815-12-10"})
    assert apply.profile_value(profile, "middle_name") == "Byron"
    assert apply.profile_value(profile, "birthday") == "1815-12-10"
    assert apply.profile_value(profile, "phone_type") == "Mobile"
    assert apply.profile_value(profile, "phone_country") == "United Kingdom"
    assert apply.profile_value(profile, "no") == "No"
    assert apply.profile_value(profile, "step") == ""
    assert apply.pick_option(apply.profile_value(profile, "no"), ["Yes", "No"]) == "No"

    assert apply.rule_for("Where you found us") == "referral_source"
    ny = apply.Profile(**{**ada_profile(), "state": "NY"})
    assert apply.profile_value(ny, "state") == "NY | New York"
    assert (
        apply.pick_option(apply.profile_value(ny, "state"), ["New Jersey", "New York"])
        == "New York"
    )
    assert apply.pick_option(apply.profile_value(ny, "state"), ["NJ", "NY"]) == "NY"

    same = apply.Profile(**{**ada_profile(), "preferred_name": "Ada"})
    assert apply.profile_value(same, "preferred_name") == ""
    other = apply.Profile(**{**ada_profile(), "preferred_name": "Countess"})
    assert apply.profile_value(other, "preferred_name") == "Countess"


def test_the_resolve_sends_the_profile_by_decision_not_by_default(client, user_headers):
    """The resolve carries the profile to the person's own extension. A new
    Profile field rides along unless excluded, so this list is the decision:
    a field added to Profile fails here until someone says it may leave the
    server (homelab's note on the d0953bb roll)."""
    client.put("/v1/user/profile", json=ada_profile(), headers=user_headers)
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
