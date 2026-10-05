import pytest

from api import db
from core.managed_board_title_gate import TitleGateConfig, evaluate, sql_for_json


def test_internship_gate_keeps_explicit_titles_and_curated_sources():
    config = TitleGateConfig(recipe="internship_v1", mode="shadow")

    assert evaluate(config, title="Software Engineering Intern", source="other").keep
    assert evaluate(config, title="PhD Resident - Multiple Teams", source="internships").keep
    assert not evaluate(config, title="Senior Software Engineer", source="other").keep


def test_new_grad_gate_rejects_internships_and_experienced_seniority():
    config = TitleGateConfig(recipe="new_grad_v1", mode="shadow")

    assert evaluate(config, title="Software Engineer - University Hire 2027", source="other").keep
    assert not evaluate(config, title="Software Engineering Intern", source="other").keep
    assert not evaluate(config, title="Principal Product Manager", source="other").keep


def test_off_gate_never_filters():
    decision = evaluate(None, title="Account Executive", source="other")
    assert decision.keep
    assert decision.reason == "disabled"


def test_aero_major_gate_keeps_the_disciplines_and_not_the_rest():
    config = TitleGateConfig(recipe="aero_major_v1", mode="shadow")

    for title in (
        "Propulsion Engineer I",
        "Structures Analyst - Stress",
        "GN&C Engineer",
        "Mechanical Design Engineer, New Grad",
        "Systems Engineer - Spacecraft",
        "Manufacturing Engineer",
    ):
        assert evaluate(config, title=title, source="other").keep, title
    for title in ("Account Executive", "Software Engineer", "Registered Nurse"):
        assert not evaluate(config, title=title, source="other").keep, title


@pytest.mark.parametrize("recipe", ["internship_v1", "new_grad_v1", "aero_major_v1"])
def test_the_sql_spelling_decides_every_title_as_python_does(f, recipe):
    """Verification reach reads the SQL spelling; a run reads the Python one.
    A recipe the SQL CASE does not name falls to ELSE FALSE and silently stops
    verifying everything the board would show."""
    titles = [
        "Propulsion Engineer I",
        "Software Engineering Intern",
        "Senior Mechanical Engineer",
        "Account Executive",
        "Co-op, Flight Software",
        "GN&C Engineer",
    ]
    ids = {f.make_job(source="other", title=title): title for title in titles}
    sql, params = sql_for_json("%(gate)s::jsonb")
    kept = {
        row["title"]
        for row in db.query(
            f"SELECT j.title FROM jobs j WHERE j.id = ANY(%(ids)s) {sql}",
            {
                "ids": list(ids),
                "gate": db.jsonb({"recipe": recipe, "mode": "enforce"}),
                **params,
            },
        )
    }
    config = TitleGateConfig(recipe=recipe, mode="enforce")
    assert kept == {t for t in titles if evaluate(config, title=t, source="other").keep}
