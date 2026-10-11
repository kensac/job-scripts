"""Resolve form fields against a person's answer bank, profile and drafts."""

from __future__ import annotations

import re

from api import db
from api.apply.fields import Field_, ResolvedField
from api.apply.options import pick_option
from api.apply.profile import CONSENT, load_profile, profile_value
from api.apply.rules import YESNO_FACTS, normalize, rule_for


def resolve(user_id: int, job_id: int | None, fields: list[Field_]) -> list[ResolvedField]:
    """One entry per field: the field as read, the rung that filled it and
    the value, or rung "" and value None when nothing did."""
    profile = load_profile(user_id)
    saved_answers_by_label = {
        row["label_norm"]: row
        for row in db.query(
            "SELECT id, label_norm, value FROM application_answer_bank WHERE user_id = %s",
            (user_id,),
        )
    }
    drafts_by_field_key: dict[str, str] = {}
    drafts_by_question: dict[str, str] = {}
    if job_id is not None:
        for row in db.query(
            "SELECT key, question, draft FROM application_answers "
            "WHERE user_id = %s AND job_id = %s AND COALESCE(draft, '') <> ''",
            (user_id, job_id),
        ):
            drafts_by_field_key[row["key"]] = row["draft"]
            drafts_by_question[normalize(row["question"])] = row["draft"]
    resolved_fields = []
    for form_field in fields:
        normalized_label = normalize(form_field.label)
        rung, value = "", None
        if form_field.kind == "file":
            rung, value = (
                ("resume", str(profile.default_resume_id))
                if (profile.default_resume_id and re.search(r"resume|cv\b", normalized_label))
                else ("", None)
            )
        elif normalized_label in saved_answers_by_label:
            rung, value = "bank", saved_answers_by_label[normalized_label]["value"]
        elif (
            form_field.fact
            and form_field.fact != "resume"
            and (profile_answer := profile_value(profile, form_field.fact))
        ):
            # The reader named the fact; no label to read.
            rung, value = "profile", profile_answer
        elif (
            (fact := rule_for(form_field.label))
            and (form_field.kind != "yesno" or fact in YESNO_FACTS)
            and (fact not in YESNO_FACTS or form_field.kind in ("yesno", "select", "multiselect"))
            and (profile_answer := profile_value(profile, fact))
        ):
            rung, value = "profile", profile_answer
        # A consent with one box is that box, whatever it is called.
        if (
            rung == "profile"
            and value == CONSENT
            and form_field.kind == "multiselect"
            and len(form_field.options) == 1
        ):
            value = form_field.options[0]
        elif form_field.key in drafts_by_field_key:
            rung, value = "draft", drafts_by_field_key[form_field.key]
        elif normalized_label in drafts_by_question:
            rung, value = "draft", drafts_by_question[normalized_label]
        hint = None
        if value is not None and form_field.kind not in ("select", "yesno", "multiselect"):
            # Alternatives are for choosing among options; a text box takes
            # the first.
            value = value.split("|")[0].strip()
        if (
            value is not None
            and form_field.kind in ("select", "yesno", "multiselect")
            and form_field.options
        ):
            picked = pick_option(value, form_field.options)
            if picked is None:
                # The fact is known but none of the options is recognisably
                # it; the hint goes to the model with the options.
                hint, rung, value = value, "", None
            else:
                value = picked
        resolved_fields.append(
            ResolvedField(**form_field.model_dump(), rung=rung, value=value, hint=hint)
        )
    return resolved_fields
