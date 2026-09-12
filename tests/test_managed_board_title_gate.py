from core.managed_board_title_gate import TitleGateConfig, evaluate


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
