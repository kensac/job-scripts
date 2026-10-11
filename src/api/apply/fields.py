"""Form fields read by the extension and resolved by the server."""

from __future__ import annotations

from pydantic import BaseModel, Field

ApplicationOptions = list[str]


class Field_(BaseModel):
    """One field as the extension read it. kind is the widget: text, long,
    select, yesno, file, number, date. options are the select's choices
    as shown, in order."""

    key: str = Field(min_length=1, max_length=300)
    # A consent's label is its whole paragraph; 500 refused a real form.
    label: str = Field(default="", max_length=4000)
    kind: str = Field(default="text", max_length=20)
    required: bool = False
    options: ApplicationOptions = Field(default_factory=list)
    # A config-driven reader knows which fact a selector fills (first_name,
    # needs_sponsorship, resume); when it says so, the label is not read.
    fact: str | None = Field(default=None, max_length=40)


class ResolvedField(Field_):
    """The field as read, plus what `resolve` decided about it.

    Declared here rather than in the router that returns it, because the
    three keys are this function's answer and a router that copies them is
    how the two come apart. `rung` is which source filled it (profile, bank,
    draft, resume) and is "" when nothing did; `hint` is the person's own
    answer when the options did not recognisably hold it, which is what the
    model is given instead of the value.
    """

    rung: str
    value: str | None
    hint: str | None
