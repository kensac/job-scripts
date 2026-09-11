"""The closed vocabularies a posting's requirements are recorded in.

Lives in core because the extraction and the API must not each keep their own
copy: the extraction validates the model's answer against them, and the market
endpoint orders its degree and clearance tables by POSITION in these tuples. A
model answering "Bachelor's degree" and one answering "bachelors" have to land
on the same token or the counts are split across two spellings of one level.
"""

from __future__ import annotations

from pydantic import BaseModel

# Ordered least to most, because degree and clearance requirements are FLOORS.
# A posting asking for "a Bachelor's or Master's" states a floor of bachelors,
# and one asking for "Secret or above" states a floor of secret. Ordering is
# what lets a reader see the market's floors as a ladder - array_position
# rather than alphabetical, which would put "bachelors" above "phd".
DEGREE_LEVELS = ("none", "high_school", "associate", "bachelors", "masters", "phd")
CLEARANCE_LEVELS = ("none", "public_trust", "confidential", "secret", "top_secret", "ts_sci")

# Unordered: a posting is one of these, not at least one of them. Seniority
# reads like a ladder but is not comparable in the same way - a staff engineer
# role is not "more than" a manager role, and treating it as one would drop
# postings out of a slice for a reason nobody stated.
SENIORITIES = (
    "intern",
    "new_grad",
    # Not in the first draft of this vocabulary. The pilot's model answered
    # "postdoc" for two of 60 postings, which is the corpus saying it holds a
    # category the list was missing rather than the model drifting - research
    # roles are a real slice of this market, and dropping them to NULL would
    # have hidden them instead of counting them.
    "postdoc",
    "entry",
    "mid",
    "senior",
    "staff",
    "principal",
    "manager",
    "executive",
)
EMPLOYMENT_TYPES = ("intern", "full_time", "part_time", "contract", "temporary")
SPONSORSHIPS = ("offered", "not_offered")

SKILL_KINDS = ("required", "preferred")


# A full working life, 18 to 68. Nothing above this can be a number of years of
# professional experience, so a larger value is the model having read a salary,
# a requisition number or a calendar year off the page. comp.py bounds its
# annual figure for the same reason: a wrong number in a sortable column is
# worse than a missing one, because it silently reorders every answer built on
# it, and nobody can see that it did.
MAX_PLAUSIBLE_YOE = 50


def in_vocabulary(value: str | None, allowed: tuple[str, ...]) -> str | None:
    """A vocabulary token, or None when the value is outside it.

    None and an out-of-vocabulary string both mean "we do not know", but only
    None says so to every reader. Letting 'Bachelors Degree' through would give
    the aggregate a bucket of one that looks like a finding.
    """
    text = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return text if text in allowed else None


# Postings are truncated before they reach the model. 20k characters covers all
# but 24 of the 20,730 pages in the corpus, and the tail past that is boilerplate
# (similar-role lists, cookie notices) rather than requirements.
REQUIREMENTS_INPUT_CHARS = 20000


class RequirementsExtract(BaseModel):
    """What the posting STATES, not what the role probably wants.

    Every field is optional-by-omission because absence is the most common
    answer in this corpus and is itself the finding: a market where 60% of
    postings never name a degree is a different market from one where they all
    demand a bachelor's, and a schema that cannot say "unstated" collapses the
    two. Values are validated against the vocabularies above in Python rather
    than pinned by the JSON schema, so a drifting model answer can be corrected
    without re-running a paid pass.
    """

    has_requirements: bool
    yoe_min: int | None = None
    yoe_max: int | None = None
    degree_min: str = ""
    degree_required: bool = False
    degree_fields: list[str] = []
    enrollment_required: bool = False
    seniority: str = ""
    employment_type: str = ""
    clearance: str = ""
    citizenship_required: bool = False
    sponsorship: str = ""
    skills_required: list[str] = []
    skills_preferred: list[str] = []


REQUIREMENTS_INSTRUCTIONS = (
    "Extract what THIS job posting requires of a candidate. Report only what the "
    "employer states about this role. The page may also carry aggregator "
    "commentary, company news, funding history, other applicants, benefits, "
    "'similar jobs' listings and an application form: none of that is a "
    "requirement of this role.\n"
    "has_requirements: true only when the page states qualifications for this "
    "role. False for a bare application form, a login wall, an error page, or a "
    "listing with a description but no qualifications. When false, leave every "
    "other field empty.\n"
    "yoe_min/yoe_max: years of professional experience required, as numbers. "
    "'3+ years' is min 3 with no max; '3-5 years' is min 3 max 5; '5 years' is "
    "min 5 with no max; '0-3 years' or 'up to 3 years' is min 0 max 3. Always "
    "give yoe_min when you give yoe_max. When several are named for different "
    "skills, report the lowest that is required of the candidate overall. Leave "
    "both empty when no number of years is stated - do not infer years from a "
    "seniority word.\n"
    "degree_min: the LOWEST degree that qualifies, exactly one of none, "
    "high_school, associate, bachelors, masters, phd. 'Bachelor's or Master's' "
    "is bachelors. Leave EMPTY when the posting does not mention a degree at "
    "all; use none only when it says outright that no degree is required.\n"
    "degree_required: true when the degree is a requirement, false when it is "
    "preferred, 'a plus', or listed among nice-to-haves.\n"
    "degree_fields: fields of study named, e.g. ['Computer Science', "
    "'Electrical Engineering']. Empty when the posting names no field.\n"
    "enrollment_required: true only when the posting requires the candidate to "
    "be a currently enrolled student, or to be returning to study afterwards.\n"
    "seniority: exactly one of intern, new_grad, postdoc, entry, mid, senior, staff, "
    "principal, manager, executive. new_grad only for roles explicitly aimed at "
    "recent or upcoming graduates. Empty when the posting gives no signal.\n"
    "employment_type: exactly one of intern, full_time, part_time, contract, "
    "temporary. An internship is intern, not full_time, however many hours a "
    "week it runs. Empty when unstated.\n"
    "clearance: the security clearance required, exactly one of none, "
    "public_trust, confidential, secret, top_secret, ts_sci. Empty when the "
    "posting does not mention clearance at all; use none only when it says "
    "outright that no clearance is required.\n"
    "citizenship_required: true only when the posting requires US citizenship "
    "or permanent residency.\n"
    "sponsorship: offered when the employer states it sponsors visas for this "
    "role, not_offered when it states it will not. Empty otherwise. An "
    "application-form question asking whether the candidate needs sponsorship "
    "is NOT a statement either way. Neither is a third-party estimate of the "
    "company's sponsorship history.\n"
    "skills_required: technologies, tools, languages, frameworks, platforms and "
    "named technical methods the posting lists as required. Each entry is a "
    "NAME of at most four words, written as the posting writes it: 'Python', "
    "'Kubernetes', 'AutoCAD', 'Momentum ERP', 'finite element analysis'. Never "
    "a phrase or a sentence - write 'Plaxis', not 'Experience with Plaxis'. "
    "Exclude behavioural and interpersonal qualities (communication, teamwork, "
    "leadership, organisation, problem solving, attention to detail, work "
    "ethic, adaptability), degrees, fields of study, years of experience, "
    "clearances, certifications of eligibility to work, and job titles.\n"
    "skills_preferred: the same, for anything the posting marks as preferred, "
    "desired, bonus or nice-to-have. A skill belongs in exactly one of the two "
    "lists; when the posting does not separate them, treat them as required."
)
