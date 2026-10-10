"""Title screens: named recipes that decide from a posting's title and source alone.

A recipe is an ordered list of rules. The first rule that matches decides; a
title no rule matches takes the recipe's default. `screen` reads the list in
Python and `skips_sql` writes the same list as a SQL `CASE`, so the two
spellings cannot drift: a test holds them equal on every recipe.

**A recipe is never edited in place.** Every stored verdict, recall measurement
and enforcement decision was made against a recipe as it stood, and editing it
would silently change what those measured. A different rule is a new recipe
name, measured before it is enforced. `tests/test_screening.py` pins a digest
of every recipe, so an edit fails there.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

Recipe = Literal[
    "internship_v1",
    "new_grad_v1",
    "aero_major_v1",
    "nontechnical_occupations_v1",
    "occupation_words_v1",
]
BoardRecipe = Literal["internship_v1", "new_grad_v1", "aero_major_v1"]


@dataclass(frozen=True)
class Screen:
    skip: bool
    reason: str


@dataclass(frozen=True)
class _Rule:
    """Matches a title pattern, or a source in a fixed set (exactly one)."""

    decides: Screen
    title: re.Pattern[str] | None = None
    sources: frozenset[str] = frozenset()


def _keep(
    reason: str, *, title: re.Pattern[str] | None = None, sources: frozenset[str] = frozenset()
) -> _Rule:
    return _Rule(Screen(False, reason), title, sources)


def _skip(reason: str, *, title: re.Pattern[str] | None = None) -> _Rule:
    return _Rule(Screen(True, reason), title)


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# A technical qualifier wins over an occupation match. Broad titles such as
# analyst, associate, manager, technician and operations never reject alone.
_TECHNICAL = _words(
    r"\b(?:engineer\w*|software|developer\w*|data|analyst\w*|product|program|"
    r"technical|technology|computer|computing|systems?|infrastructure|platform|"
    r"automation|robot\w*|firmware|embedded|machine learning|artificial intelligence|"
    r"research|scientist|science|cyber\w*|devops|sre|it|ux|ui|solutions?|"
    r"consult\w*|quant\w*|ai|ml)\b"
)

_CURATED_INTERNSHIP_SOURCES = frozenset(
    {"internships", "speedyapply_intern", "speedyapply_ai_intern", "internships_ouckah"}
)
_INTERNSHIP_SIGNAL = _words(
    r"\b(?:intern(?:ship)?s?|co[ -]?op|student|university|summer (?:analyst|associate)|intensive program)\b"
)
_EXPLICIT_INTERNSHIP = _words(
    r"\b(?:intern(?:ship)?s?|co[ -]?op|summer (?:analyst|associate)|intensive program)\b"
)
_EXPERIENCED = _words(r"\b(?:senior|sr\.?|principal|lead|director|head|vice president|vp|chief)\b")
# Work an aerospace engineering degree prepares for, loosely: the field's own
# words plus the mechanical, structural, controls and systems disciplines it
# shares. Level and fit are the model's call; this only keeps the call off
# postings no aerospace major would read. Sized on production 2026-10-05: 1,175
# of 26,991 verified-open postings in a week (4.4%). On 2026-10-07 it would
# have dropped 70 of the Aerospace board's 268 keeps, so it is not enforced.
_AERO_MAJOR_SIGNAL = _words(
    r"\b(?:aero\w*|astro\w*|avionics?|propulsion|spacecraft|satellites?|launch|rockets?"
    r"|flight|gn&?c|guidance|aircraft|airframes?|orbital|mechanical|structur(?:al|es)|stress"
    r"|thermal|fluids?|cfd|aerodynamics?|controls?|dynamics|mechatronics?|composites?"
    r"|materials|reliability|(?:systems?|test|manufacturing|integration|design|hardware"
    r"|quality|process|industrial|mission) engineer\w*)\b"
)

# Occupations no board or filter has kept, as whole title words. Derived
# 2026-10-07 from every custom verdict since 2026-08-01 (all prompts): a word in
# at least 300 rejected non-technical titles across at least 10 sources and in
# no kept non-technical title. Built from verdicts before 2026-10-01, it dropped
# 0 of 1,129 keeps after; the 47 below are that list less its street names,
# brands and generic words, and drop 0 of 27,241 keeps over all history. The
# technical words still win, so "Nurse Informatics Engineer" is reviewed.
_OCCUPATION_WORDS = (
    "accountant", "accounting", "aide", "auditor", "baker", "bakery", "behavioral",
    "beverage", "billing", "cardiology", "cdl", "chef", "cleaner", "clinician", "cook",
    "diesel", "expeditor", "forklift", "icu", "infusion", "inpatient", "janitorial",
    "kitchen", "loader", "lpn", "neurology", "nurse", "pathologist", "patient", "payroll",
    "pediatrics", "pharmacist", "physician", "picker", "porter", "practitioner",
    "recruiting", "respiratory", "secretary", "sous", "speech", "teacher", "teller",
    "therapist", "therapy", "underwriter", "urology",
)  # fmt: skip

RECIPES: dict[str, tuple[tuple[_Rule, ...], Screen]] = {
    # Tech Internships, enforced: measured 2026-10-05, it skips 96.7% of calls
    # and dropped 7 of 1,494 model keeps.
    "internship_v1": (
        (
            _keep("curated_internship_source", sources=_CURATED_INTERNSHIP_SOURCES),
            _keep("internship_title_signal", title=_INTERNSHIP_SIGNAL),
        ),
        Screen(True, "no_internship_title_signal"),
    ),
    # Would drop 59 of Tech New Grad's 1,068 keeps (2026-10-07): not enforced.
    "new_grad_v1": (
        (
            _skip("internship_title_signal", title=_EXPLICIT_INTERNSHIP),
            _skip("experienced_title_signal", title=_EXPERIENCED),
        ),
        Screen(False, "possible_new_grad_title"),
    ),
    "aero_major_v1": (
        (_keep("aero_major_title_signal", title=_AERO_MAJOR_SIGNAL),),
        Screen(True, "no_aero_major_title_signal"),
    ),
    # Explicit occupations only, for filters and boards in title_screens.
    "nontechnical_occupations_v1": (
        (
            _keep("technical_title", title=_TECHNICAL),
            _skip(
                "clinical_care",
                title=_words(
                    r"\b(?:registered nurse|licensed (?:practical|vocational) nurse|nursing assistant|"
                    r"nurse practitioner|phlebotomist|pharmacist|dental hygienist|"
                    r"physical therapist|occupational therapist|radiologic technologist)\b"
                ),
            ),
            _skip(
                "retail_service",
                title=_words(
                    r"\b(?:cashier|retail sales associate|store manager|customer service rep(?:resentative)?|"
                    r"deli clerk|night crew stocker|in-store shopper)\b"
                ),
            ),
            _skip(
                "driving", title=_words(r"\b(?:delivery driver|commercial driver|truck driver)\b")
            ),
            _skip(
                "hospitality_cleaning",
                title=_words(
                    r"\b(?:janitorial cleaner|custodial cleaner|custodian|housekeeper|dishwasher|"
                    r"bartender|line cook|prep cook|restaurant server)\b"
                ),
            ),
        ),
        Screen(False, "no_listed_occupation"),
    ),
    # The verification volume gate's list (core.volume_gate.VolumeGate).
    "occupation_words_v1": (
        (
            _keep("technical_title", title=_TECHNICAL),
            _skip(
                "unrelated_occupation",
                title=_words(r"\b(?:" + "|".join(_OCCUPATION_WORDS) + r")\b"),
            ),
        ),
        Screen(False, "no_listed_occupation"),
    ),
}


def screen(recipe: Recipe, *, title: str | None, source: str | None) -> Screen:
    rules, default = RECIPES[recipe]
    text = title or ""
    for rule in rules:
        if rule.title.search(text) if rule.title else source in rule.sources:
            return rule.decides
    return default


def _param(recipe: str, index: int) -> str:
    return f"screening_{recipe}_{index}"


def _postgres(pattern: re.Pattern[str]) -> str:
    # Python spells a word boundary \b; PostgreSQL's ARE engine spells the
    # same assertion \y. Everything else in these recipes is shared. Matching
    # is case-insensitive in both: re.IGNORECASE here, ~* there.
    return pattern.pattern.replace(r"\b", r"\y")


PARAMS: dict[str, object] = {
    _param(name, n): _postgres(rule.title) if rule.title else sorted(rule.sources)
    for name, (rules, _) in RECIPES.items()
    for n, rule in enumerate(rules)
}


def skips_sql(recipe: str, *, title: str = "j.title", source: str = "j.source") -> str:
    """A boolean SQL expression: does `recipe` (a SQL text expression) skip the row?

    A NULL recipe skips nothing. Pass `PARAMS` with the statement.
    """
    arms = []
    for name, (rules, default) in RECIPES.items():
        whens = " ".join(
            f"WHEN COALESCE({title}, '') ~* %({_param(name, n)})s THEN {rule.decides.skip}"
            if rule.title
            else f"WHEN {source} = ANY(%({_param(name, n)})s::text[]) THEN {rule.decides.skip}"
            for n, rule in enumerate(rules)
        )
        arms.append(f"WHEN '{name}' THEN (CASE {whens} ELSE {default.skip} END)")
    return f"(CASE {recipe} {' '.join(arms)} ELSE FALSE END)"


def digest(recipe: str) -> str:
    """What the pinning test holds fixed: every rule, in order, and the default."""
    rules, default = RECIPES[recipe]
    spelled = [
        [r.decides.skip, r.decides.reason, r.title.pattern if r.title else sorted(r.sources)]
        for r in rules
    ] + [[default.skip, default.reason]]
    return hashlib.sha256(json.dumps(spelled).encode()).hexdigest()[:16]


class TitleGateConfig(BaseModel):
    """A managed board's title gate (`managed_boards.title_gate`).

    A gate that is set is enforced. A shadow mode used to judge every
    candidate anyway and report what the gate would drop; that is a query over
    the board's verdicts and `screen`, so a recipe is measured that way before
    a board names it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe: BoardRecipe
    # Stored rows carry the key; "enforce" is the only value left.
    mode: Literal["enforce"] = "enforce"
