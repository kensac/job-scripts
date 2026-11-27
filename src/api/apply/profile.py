"""A person's application facts and values derived from them."""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from api import user_settings

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
    middle_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    preferred_name: str = Field(default="", max_length=100)
    pronouns: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=40)
    # "Mobile", "Home", "Work": the phone type pickers on the older ATSs.
    phone_type: str = Field(default="Mobile", max_length=20)
    # YYYY-MM-DD; asked by government and enterprise forms, never required
    # here. The engine formats it by the field's name.
    birthday: str = Field(default="", max_length=10)
    address: str = Field(default="", max_length=200)
    address_2: str = Field(default="", max_length=200)
    address_3: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=100)
    state: str = Field(default="", max_length=100)
    postal_code: str = Field(default="", max_length=20)
    country: str = Field(default="", max_length=100)
    linkedin: str = Field(default="", max_length=300)
    github: str = Field(default="", max_length=300)
    website: str = Field(default="", max_length=300)
    twitter: str = Field(default="", max_length=300)
    behance: str = Field(default="", max_length=300)
    dribbble: str = Field(default="", max_length=300)
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


# Choices belong to the source form, not a fixed-size vocabulary. Keep the
# complete list in both request paths so a valid answer cannot be cut off.
ApplicationOptions = list[str]
CONSENT = "Yes | I agree | I accept | I acknowledge | I consent | I confirm | I certify"


def _preferred_name(profile: Profile) -> str:
    # A preferred name that is the first name is no preferred name: Workday
    # ticked "I have a preferred name" and filled it (2026-09-08).
    pref = profile.preferred_name.strip()
    return "" if pref.lower() == profile.first_name.strip().lower() else pref


def _state_value(profile: Profile) -> str:
    # A state box may list abbreviations or full names; offer both.
    if not profile.state:
        return ""
    full = US_STATES.get(profile.state.upper())
    return (
        f"{profile.state} | {full}"
        if full and full.lower() != profile.state.lower()
        else profile.state
    )


def _experience_fact(profile: Profile, index: int, attr: str) -> str:
    if len(profile.experience) <= index:
        return ""
    return getattr(profile.experience[index], attr)


def _education_fact(profile: Profile, attr: str) -> str:
    if not profile.education:
        return ""
    return getattr(profile.education[0], attr) or ""


_STATIC_FACTS: dict[str, str] = {
    "consent": CONSENT,
    "decline": DECLINE,
    "yes": "Yes",
    "no": "No",
    # A flow step in a config-driven reader (a begin or save button, a wait)
    # carries no value; the engine runs it at its place in the order.
    "step": "",
}

_DERIVED_FACTS: dict[str, Callable[[Profile], str]] = {
    "experience": lambda p: f"{len(p.experience)} entries" if p.experience else "",
    "education": lambda p: f"{len(p.education)} entries" if p.education else "",
    "preferred_name": _preferred_name,
    # The phone's country picker: the person's country.
    "phone_country": lambda p: p.country,
    "today": lambda _p: datetime.datetime.now(datetime.UTC).date().isoformat(),
    "phone_digits": lambda p: re.sub(r"\D", "", p.phone),
    "full_name": lambda p: p.full_name,
    "location": lambda p: p.location,
    "state": _state_value,
    "current_company": lambda p: _experience_fact(p, 0, "company"),
    "current_title": lambda p: _experience_fact(p, 0, "title"),
    "previous_company": lambda p: _experience_fact(p, 1, "company"),
    "previous_title": lambda p: _experience_fact(p, 1, "title"),
    "school": lambda p: _education_fact(p, "school"),
    "degree": lambda p: _education_fact(p, "degree"),
    "field_of_study": lambda p: _education_fact(p, "field"),
    "gpa": lambda p: _education_fact(p, "gpa"),
    "graduation": lambda p: _education_fact(p, "end"),
}


def profile_value(profile: Profile, fact: str) -> str:
    """The profile's answer for a fact, "" when it has none. A few facts are
    not on the profile: consent (always given), today's date, "yes" (a
    config-driven reader's over-18 check), the phone as digits."""
    if fact in _STATIC_FACTS:
        return _STATIC_FACTS[fact]
    if handler := _DERIVED_FACTS.get(fact):
        return handler(profile)
    return str(getattr(profile, fact, "") or "")


US_STATES = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "PR": "Puerto Rico",
}


def load_profile(user_id: int) -> Profile:
    return Profile.model_validate(user_settings.profile(user_id))
