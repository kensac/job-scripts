import pytest

from api import db
from core import screening
from core.screening import screen

# A recipe is never edited in place (core/screening.py). A failure here means a
# rule, its order, a reason or a default changed: add a new recipe name
# instead, and measure it before anything enforces it.
PINNED = {
    "internship_v1": "19271263cd35ee1b",
    "new_grad_v1": "a44a8ef1573bc1c8",
    "aero_major_v1": "93738b838f66509e",
    "nontechnical_occupations_v1": "6e941463d4697cd6",
    "occupation_words_v1": "fbb7575ba9e69efe",
}

# Titles each recipe decides both ways, plus the awkward shapes production
# holds: no title, blank, punctuation, mixed case.
TITLES = [
    None,
    "",
    "   ",
    "Software Engineering Intern",
    "Co-op, Flight Software",
    "PhD Resident - Multiple Teams",
    "Software Engineer - University Hire 2027",
    "Senior Software Engineer",
    "Sr. Mechanical Engineer",
    "Principal Product Manager",
    "Propulsion Engineer I",
    "Structures Analyst - Stress",
    "GN&C Engineer",
    "Systems Engineer - Spacecraft",
    "Account Executive",
    "Registered Nurse (RN) - ICU",
    "Retail Sales Associate - Part Time",
    "Delivery Driver (123) - Main Street",
    "JANITORIAL CLEANER",
    "Phlebotomist II",
    "Data Engineer - Retail Sales Associate Tools",
    "Registered Nurse - Clinical Systems Analyst",
    "Nurse Informatics Engineer",
    "Line Cook",
    "Forklift Operator",
    "Payroll Specialist",
    "Teller, Part Time",
]
SOURCES = ["other", "internships", None]


def test_no_recipe_is_edited_in_place():
    assert {name: screening.digest(name) for name in screening.RECIPES} == PINNED


@pytest.mark.parametrize("recipe", sorted(screening.RECIPES))
def test_the_sql_spelling_decides_every_title_as_python_does(recipe):
    """Candidate selection and verification reach read the SQL; runs and the
    posting path read Python. Both come from one rule list, and this holds the
    generator to it."""
    pairs = [(title, source) for title in TITLES for source in SOURCES]
    rows = db.query(
        "SELECT n, "
        + screening.skips_sql("%(recipe)s::text", title="t.title", source="t.source")
        + " AS skip FROM unnest(%(titles)s::text[], %(sources)s::text[]) "
        "WITH ORDINALITY AS t(title, source, n)",
        {
            "recipe": recipe,
            "titles": [title for title, _ in pairs],
            "sources": [source for _, source in pairs],
            **screening.PARAMS,
        },
    )
    sql = {row["n"] - 1: row["skip"] for row in rows}
    python = {
        n: screen(recipe, title=title, source=source).skip
        for n, (title, source) in enumerate(pairs)
    }
    assert sql == python
    # Each recipe sees both outcomes here, or the comparison proved nothing.
    assert set(python.values()) == {True, False}


def test_a_null_recipe_skips_nothing():
    row = db.query_one(
        "SELECT " + screening.skips_sql("NULL::text", title="'Line Cook'", source="'x'") + " AS s",
        screening.PARAMS,
    )
    assert row is not None and row["s"] is False


def test_board_recipes():
    assert not screen("internship_v1", title="Software Engineering Intern", source="other").skip
    assert not screen("internship_v1", title="PhD Resident", source="internships").skip
    assert screen("internship_v1", title="Senior Software Engineer", source="other").skip
    assert not screen("new_grad_v1", title="Software Engineer - University Hire", source="o").skip
    assert screen("new_grad_v1", title="Software Engineering Intern", source="o").reason == (
        "internship_title_signal"
    )
    assert screen("new_grad_v1", title="Principal Product Manager", source="o").skip
    for title in ("Propulsion Engineer I", "GN&C Engineer", "Manufacturing Engineer"):
        assert not screen("aero_major_v1", title=title, source="o").skip, title
    for title in ("Account Executive", "Software Engineer", "Registered Nurse"):
        assert screen("aero_major_v1", title=title, source="o").skip, title


@pytest.mark.parametrize(
    "title",
    [
        "Registered Nurse (RN) - ICU",
        "Retail Sales Associate - Part Time",
        "Delivery Driver (123) - Main Street",
        "JANITORIAL CLEANER",
        "Phlebotomist II",
    ],
)
def test_explicit_unrelated_occupations_are_skipped(title):
    assert screen("nontechnical_occupations_v1", title=title, source=None).skip


@pytest.mark.parametrize(
    "title",
    [
        "",
        "Analyst",
        "Operations Associate",
        "Program Manager",
        "Technician",
        "Device Driver Software Engineer",
        "Data Engineer - Retail Sales Associate Tools",
        "Product Manager - Nurse Platform",
        "Research Scientist",
        "ML Infrastructure Intern",
        "Finance Data Analyst",
        "Registered Nurse - Clinical Systems Analyst",
    ],
)
def test_ambiguous_or_technical_titles_are_reviewed(title):
    assert not screen("nontechnical_occupations_v1", title=title, source=None).skip


def test_occupation_words_yield_to_a_technical_word():
    assert screen("occupation_words_v1", title="Forklift Operator", source=None).skip
    assert not screen("occupation_words_v1", title="Nurse Informatics Engineer", source=None).skip
