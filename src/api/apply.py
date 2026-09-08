"""What goes in each field of an application form.

The extension reads a form and sends every field's label, kind and options.
Each field climbs the same ladder, and the ledger records which rung filled
it:

1. bank     the person typed an answer to this exact label before
2. profile  the label names one of the facts the profile holds
3. draft    the label is a question with a draft in application_answers
4. ai       the extension asks the model once for everything still blank
5. (blank)  nothing fits; the person fills it and the bank remembers it

The profile rules are a table of regexes, not a model call: a form's labels
are the same twenty facts in a hundred phrasings, and the fills ledger says
which phrasings the table misses, so the table grows from evidence rather
than from a classifier whose misses nobody can see.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from api import db

DECLINE = "Decline to self-identify"


class Experience(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    start: str | None = Field(default=None, max_length=20)
    end: str | None = Field(default=None, max_length=20)
    current: bool = False
    description: str | None = Field(default=None, max_length=4000)


class Education(BaseModel):
    school: str = Field(min_length=1, max_length=200)
    degree: str | None = Field(default=None, max_length=120)
    field: str | None = Field(default=None, max_length=120)
    start: str | None = Field(default=None, max_length=20)
    end: str | None = Field(default=None, max_length=20)
    gpa: str | None = Field(default=None, max_length=10)


class Profile(BaseModel):
    """Every fact a form asks that is not a question about the job. Blank
    means unknown, and an unknown fact leaves its field blank rather than
    guessing. EEO answers default to declining."""

    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    preferred_name: str = Field(default="", max_length=100)
    pronouns: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
    postal_code: str = Field(default="", max_length=20)
    country: str = Field(default="", max_length=100)
    linkedin: str = Field(default="", max_length=300)
    github: str = Field(default="", max_length=300)
    website: str = Field(default="", max_length=300)
    twitter: str = Field(default="", max_length=300)
    # Free text: "U.S. citizen", "F-1 OPT", "H-1B"; forms ask it beside
    # authorisation and sponsorship.
    visa_status: str = Field(default="", max_length=120)
    # "How did you hear about us?": one answer for every form.
    referral_source: str = Field(default="", max_length=120)
    work_authorized: Literal["", "yes", "no"] = ""
    needs_sponsorship: Literal["", "yes", "no"] = ""
    # The two yes/no questions every posting asks in its own words.
    willing_to_relocate: Literal["", "yes", "no"] = ""
    willing_onsite: Literal["", "yes", "no"] = ""
    # Anything else the model should know when it answers a question the
    # rules cannot: "always willing to relocate; any number of days in an
    # office". Free text, the person's own words.
    notes: str = Field(default="", max_length=2000)
    years_experience: str = Field(default="", max_length=10)
    desired_salary: str = Field(default="", max_length=60)
    start_date: str = Field(default="", max_length=100)
    gender: str = Field(default=DECLINE, max_length=80)
    ethnicity: str = Field(default=DECLINE, max_length=80)
    hispanic: str = Field(default=DECLINE, max_length=80)
    veteran: str = Field(default=DECLINE, max_length=80)
    disability: str = Field(default=DECLINE, max_length=80)
    default_resume_id: int | None = None
    experience: list[Experience] = Field(default_factory=list, max_length=30)
    education: list[Education] = Field(default_factory=list, max_length=10)

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)

    @property
    def location(self) -> str:
        full = ", ".join(p for p in (self.city, self.state, self.country) if p)
        return " | ".join(p for p in (full, self.city, self.state) if p)


class Field_(BaseModel):
    """One field as the extension read it. kind is the widget: text, long,
    select, yesno, file, number, date. options are the select's choices
    as shown, in order."""

    key: str = Field(min_length=1, max_length=300)
    # A consent's label is its whole paragraph; 500 refused a real form.
    label: str = Field(default="", max_length=4000)
    kind: str = Field(default="text", max_length=20)
    required: bool = False
    options: list[str] = Field(default_factory=list, max_length=200)
    # A config-driven reader knows which fact a selector fills (first_name,
    # needs_sponsorship, resume); when it says so, the label is not read.
    fact: str | None = Field(default=None, max_length=40)


def normalize(label: str) -> str:
    """The label with everything that is not a word removed, so "Phone
    Number *" and "phone number" are the same answer."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", label.lower()).split())


# Ordered: the first pattern that matches the normalised label names the
# profile fact. Specific phrasings sit above the general ones they contain
# ("first name" above "name"; "preferred name" above both).
_RULES: tuple[tuple[str, str], ...] = (
    (r"\bpreferred (first )?name\b", "preferred_name"),
    (r"\bpronouns?\b", "pronouns"),
    (r"\b(first|given) name\b", "first_name"),
    (r"\b(last|family) name\b|\bsurname\b", "last_name"),
    (r"^(full |legal |your )?name$|\bfull name\b|\blegal name\b", "full_name"),
    (r"\be ?mail\b", "email"),
    (r"\b(phone|mobile|cell|telephone|contact number)\b", "phone"),
    (r"\blinked ?in\b", "linkedin"),
    (r"\bgit ?hub\b", "github"),
    (r"\btwitter\b|\bx (handle|profile|url)\b", "twitter"),
    (r"\b(portfolio|website|personal site|web site|url|links?)\b", "website"),
    # Questions before places: "authorized to work in the country where the
    # job is located" is about authorisation, not a country.
    (r"\bsponsor", "needs_sponsorship"),
    (r"\brelocat", "willing_to_relocate"),
    (
        r"\b(on ?site|in ?office|in the office|from (our|the|an) office|in person|days? (a|per) week|hybrid)\b",
        "willing_onsite",
    ),
    (r"\bvisa (status|type)\b|\bimmigration status\b", "visa_status"),
    (
        r"\b(hear|heard|find out|learn|discover)\b.*\babout\b|\bhow did you (find|discover)\b",
        "referral_source",
    ),
    (
        r"\b(authori[sz]ed|eligible|legally|right) to work\b|\bwork authori[sz]ation\b",
        "work_authorized",
    ),
    (r"\byears\b.*\bexperience\b", "years_experience"),
    (
        r"\b(salary|compensation|pay) (expectation|requirement|range)s?\b"
        r"|\bdesired (salary|pay)\b|\bexpected (salary|pay|compensation)\b",
        "desired_salary",
    ),
    (
        r"\bstart date\b|\b(available|able) to (start|join)\b|\bwhen (can|could|are you able to) you start\b"
        r"|\bearliest\b.*\b(start|join)\b|\bavailability\b",
        "start_date",
    ),
    (r"\bhispanic\b|\blatin", "hispanic"),
    (r"\bgender\b|\bsex\b", "gender"),
    (r"\b(race|ethnicit)", "ethnicity"),
    (r"\bveteran\b|\bmilitary\b", "veteran"),
    (r"\bdisabilit", "disability"),
    # The person's standing instruction (2026-09-08): every field that has
    # to be filled is filled, consents included. The extension relays the
    # consent of the one person it fills for.
    (
        r"\b(acknowledg|consent|certif|agree|privacy notice|terms and conditions|arbitration)",
        "consent",
    ),
    (r"\b(previous|prior|last) (employer|company)\b", "previous_company"),
    (r"\b(previous|prior|last) (title|role|position)\b", "previous_title"),
    (
        r"\b(current|most recent|present)( or last)? (employer|company)\b|\bcompany name\b"
        r"|\bemployer\b|\bmost recently worked\b|\bcurrent last company\b",
        "current_company",
    ),
    (r"\b(current|most recent|present) (title|role|position)\b|\bjob title\b", "current_title"),
    (r"\b(school|university|college|institution|alma mater)\b", "school"),
    (r"\bdegree\b", "degree"),
    (r"\b(major|field of study|discipline)\b", "field_of_study"),
    (r"\bgpa\b|\bgrade point\b", "gpa"),
    (r"\bgraduat", "graduation"),
    (r"\b(postal|zip) ?code\b|\bpostcode\b", "postal_code"),
    (r"\bstreet\b|\baddress\b", "address"),
    (r"\bcity\b|\btown\b", "city"),
    (r"\bstate\b|\bprovince\b", "state"),
    (r"\bcountry\b", "country"),
    (r"\b(location|located|based|reside|residence|where do you live)\b", "location"),
)

YESNO_FACTS = {
    "work_authorized",
    "needs_sponsorship",
    "willing_to_relocate",
    "willing_onsite",
    "consent",
    "yes",
}
YES = re.compile(r"^(yes|y|true)\b", re.I)
NO = re.compile(r"^(no|n|false)\b", re.I)
DECLINED = re.compile(
    r"decline|prefer not|do not wish|don't wish|not to (answer|say|disclose)", re.I
)


def rule_for(label: str) -> str | None:
    norm = normalize(label)
    for pattern, fact in _RULES:
        if re.search(pattern, norm):
            return fact
    return None


CONSENT = "Yes | I agree | I accept | I acknowledge | I consent | I confirm | I certify"


def profile_value(profile: Profile, fact: str) -> str:
    """The profile's answer for a fact, "" when it has none. A few facts are
    not on the profile: consent (always given), today's date, "yes" (a
    config-driven reader's over-18 check), the phone as digits."""
    if fact == "consent":
        return CONSENT
    if fact == "yes":
        return "Yes"
    if fact == "today":
        return datetime.datetime.now(datetime.UTC).date().isoformat()
    if fact == "phone_digits":
        return re.sub(r"\D", "", profile.phone)
    if fact in ("full_name", "location"):
        return getattr(profile, fact)
    if fact in ("current_company", "current_title", "previous_company", "previous_title"):
        index = 0 if fact.startswith("current") else 1
        if len(profile.experience) <= index:
            return ""
        role = profile.experience[index]
        return role.company if fact.endswith("company") else role.title
    if fact in ("school", "degree", "field_of_study", "gpa", "graduation"):
        if not profile.education:
            return ""
        edu = profile.education[0]
        return {
            "school": edu.school,
            "degree": edu.degree,
            "field_of_study": edu.field,
            "gpa": edu.gpa,
            "graduation": edu.end,
        }[fact] or ""
    return str(getattr(profile, fact, "") or "")


NEGATED = re.compile(r"\b(not|no|never|don t|do not|none)\b", re.I)


def pick_option(value: str, options: list[str]) -> str | None:
    """The option a select should land on for a profile value. A value may
    list alternatives in order of preference, "South Asian | Asian", and
    the first that lands wins. For one alternative: exact match; yes/no by
    the option's first word, and for no, an option phrased as a negation
    ("I am not a protected veteran"); the declined option for a declined
    value; then the option that starts with the value; then the one that
    contains it as whole words, so "Asian" never lands on "Caucasian".
    None when nothing fits, so the field goes to the next rung."""
    if not value or not options:
        return None
    for alternative in value.split("|"):
        picked = _pick_one(alternative.strip(), options)
        if picked is not None:
            return picked
    return None


def _pick_one(value: str, options: list[str]) -> str | None:
    if not value:
        return None
    low = value.lower()
    for opt in options:
        if opt.strip().lower() == low:
            return opt
    if YES.match(value) or NO.match(value):
        want = YES if YES.match(value) else NO
        for opt in options:
            if want.match(opt.strip()):
                return opt
        if want is NO:
            for opt in options:
                if NEGATED.search(normalize(opt)) and not DECLINED.search(opt):
                    return opt
        return None
    if DECLINED.search(value):
        for opt in options:
            if DECLINED.search(opt):
                return opt
        return None
    for opt in options:
        if opt.strip().lower().startswith(low):
            return opt
    words = re.compile(r"\b" + re.escape(normalize(value)) + r"\b")
    for opt in options:
        if words.search(normalize(opt)):
            return opt
    return None


def load_profile(user_id: int) -> Profile:
    row = db.query_one("SELECT profile FROM user_settings WHERE user_id = %s", (user_id,))
    return Profile.model_validate((row or {}).get("profile") or {})


def resolve(user_id: int, job_id: int | None, fields: list[Field_]) -> list[dict[str, Any]]:
    """One entry per field: the field as read, the rung that filled it and
    the value, or rung "" and value None when nothing did."""
    profile = load_profile(user_id)
    bank = {
        r["label_norm"]: r
        for r in db.query(
            "SELECT id, label_norm, value FROM application_answer_bank WHERE user_id = %s",
            (user_id,),
        )
    }
    drafts: dict[str, str] = {}
    by_question: dict[str, str] = {}
    if job_id is not None:
        for r in db.query(
            "SELECT key, question, draft FROM application_answers "
            "WHERE user_id = %s AND job_id = %s AND COALESCE(draft, '') <> ''",
            (user_id, job_id),
        ):
            drafts[r["key"]] = r["draft"]
            by_question[normalize(r["question"])] = r["draft"]
    out = []
    for f in fields:
        norm = normalize(f.label)
        rung, value = "", None
        if f.kind == "file":
            rung, value = (
                ("resume", str(profile.default_resume_id))
                if (profile.default_resume_id and re.search(r"resume|cv\b", norm))
                else ("", None)
            )
        elif norm in bank:
            rung, value = "bank", bank[norm]["value"]
        elif f.fact and f.fact != "resume" and (raw := profile_value(profile, f.fact)):
            # The reader named the fact; no label to read.
            rung, value = "profile", raw
        elif (
            (fact := rule_for(f.label))
            and (f.kind != "yesno" or fact in YESNO_FACTS)
            and (fact not in YESNO_FACTS or f.kind in ("yesno", "select", "multiselect"))
            and (raw := profile_value(profile, fact))
        ):
            rung, value = "profile", raw
        # A consent with one box is that box, whatever it is called.
        if (
            rung == "profile"
            and value == CONSENT
            and f.kind == "multiselect"
            and len(f.options) == 1
        ):
            value = f.options[0]
        elif f.key in drafts:
            rung, value = "draft", drafts[f.key]
        elif norm in by_question:
            rung, value = "draft", by_question[norm]
        hint = None
        if value is not None and f.kind not in ("select", "yesno", "multiselect"):
            # Alternatives are for choosing among options; a text box takes
            # the first.
            value = value.split("|")[0].strip()
        if value is not None and f.kind in ("select", "yesno", "multiselect") and f.options:
            picked = pick_option(value, f.options)
            if picked is None:
                # The fact is known but none of the options is recognisably
                # it; the hint goes to the model with the options.
                hint, rung, value = value, "", None
            else:
                value = picked
        out.append({**f.model_dump(), "rung": rung, "value": value, "hint": hint})
    return out
