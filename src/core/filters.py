from __future__ import annotations

import hashlib

ON_AMBIGUOUS_VALUES = ("keep", "filter")

AMBIGUITY_RULES = {
    "keep": (
        "should_filter=true only if the job clearly violates the criteria; false if it matches "
        "or is ambiguous (prefer false negatives, do not lose good roles)."
    ),
    "filter": (
        "should_filter=true if the job violates the criteria, OR if the posting does not give you "
        "enough information to confirm it meets them; false only when it clearly meets them. "
        "For this filter ambiguity counts as a violation -- but this never means applying a harsher "
        "threshold than the criteria state, only that unconfirmed criteria fail."
    ),
}


def build_custom_input(company: str, title: str, content: str) -> str:
    return f"Company: {company}\nJob Title: {title}\n\nJob Content:\n{content}"


def _custom_criteria_instructions(prompt: str, on_ambiguous: str) -> str:
    return f"""Evaluate a job against the user criteria below and decide whether to filter it out.

<user_criteria>
{prompt}
</user_criteria>

{AMBIGUITY_RULES.get(on_ambiguous, AMBIGUITY_RULES["keep"])}"""


def build_custom_instructions(prompt: str, on_ambiguous: str = "keep") -> str:
    """Explanation prompt and historical cache identity; preserve its bytes."""
    if not prompt:
        return ""
    return (
        _custom_criteria_instructions(prompt, on_ambiguous)
        + "\n\nreason: <=25 words citing the deciding factor (company/role/skills)."
    )


def build_custom_decision_instructions(prompt: str, on_ambiguous: str = "keep") -> str:
    if not prompt:
        return ""
    return (
        _custom_criteria_instructions(prompt, on_ambiguous)
        + '\n\nReturn only a JSON object with the boolean field "should_filter". Do not include a reason.'
    )


def compute_filter_hash(prompt: str, on_ambiguous: str = "keep") -> str:
    # Output presentation does not change which criteria a verdict answers.
    return compute_prompt_hash(build_custom_instructions(prompt, on_ambiguous))


def compute_prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
