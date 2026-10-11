"""Map application form labels to profile facts."""

from __future__ import annotations

import re


def normalize(label: str) -> str:
    """The label with everything that is not a word removed, so "Phone
    Number *" and "phone number" are the same answer."""
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", label.lower()).split())


# Ordered: the first pattern that matches the normalised label names the
# profile fact. Specific phrasings sit above the general ones they contain
# ("first name" above "name"; "preferred name" above both).
_RULES: tuple[tuple[str, str], ...] = (
    (r"\bfirst(?: name)? (?:and )?last names?\b", "full_name"),
    (r"\bpreferred (first )?name\b", "preferred_name"),
    (r"\bpronouns?\b", "pronouns"),
    (r"\b(first|given) name\b", "first_name"),
    (r"\b(last|family) name\b|\bsurname\b", "last_name"),
    (r"^(full |legal |your )?name$|\bfull name\b|\blegal name\b", "full_name"),
    (r"\be ?mail\b", "email"),
    # The type picker before the number rule takes its label.
    (r"\bphone type\b|\btype of phone\b", "phone_type"),
    (r"\b(phone|mobile|cell|telephone|contact number)\b", "phone"),
    (r"\blinked ?in\b", "linkedin"),
    (r"\bgit ?hub\b", "github"),
    (r"\btwitter\b|\bx (handle|profile|url)\b", "twitter"),
    # Named portfolios before the generic url rule takes them.
    (r"\bbehance\b", "behance"),
    (r"\bdribbble\b", "dribbble"),
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
        r"\b(hear|heard|find out|learn|discover)\b.*\babout\b|\bhow did you (find|discover)\b"
        r"|\bwhere (did )?you (found|find)\b",
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
    # Self-identification the profile does not hold: declined, which every
    # such question offers as an option.
    (r"\btransgender\b|\bsexual orientation\b|\bfirst.generation\b|\blgbt", "decline"),
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
    (r"\baddress (line )?2\b|\bapartment\b|\bsuite\b|\bunit\b|\baddress 2\b", "address_2"),
    (r"\baddress (line )?3\b", "address_3"),
    (r"\bstreet\b|\baddress\b", "address"),
    (r"\bmiddle (name|initial)\b", "middle_name"),
    (r"\b(date of birth|birth ?date|birthday|born)\b", "birthday"),
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
    "no",
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
