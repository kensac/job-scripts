from api import apply
from api.routers.apply import SuggestField


def test_large_school_dropdown_preserves_every_choice():
    # The reported university control contained 928 choices, not 200.
    options = [f"University {index}" for index in range(927)] + ["Purdue University"]
    payload = dict(key="school", label="School", kind="select", options=options)
    resolved = apply.Field_(**payload)
    suggested = SuggestField(**payload)
    assert resolved.options == suggested.options == options
    assert apply.pick_option("Purdue University", resolved.options) == "Purdue University"
