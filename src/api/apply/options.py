"""Choose an application form option from a known answer."""

from __future__ import annotations

import re

from api.apply.rules import DECLINED, NO, YES, normalize

NEGATED = re.compile(r"\b(not|no|never|don t|do not|none)\b", re.I)


def pick_option(value: str, options: list[str]) -> str | None:
    """The option a select should land on for a profile value. A value may
    list alternatives in order of preference, "South Asian | Asian", and
    the first that lands wins. For one alternative: exact match; yes/no by
    the option's first word, and for no, an option phrased as a negation
    ("I am not a protected veteran"); the declined option for a declined
    value; then the option that starts with the value; then the one that
    contains it as whole words, so "Asian" never lands on "Caucasian".
    None when nothing fits, so the field goes to the next rung."""
    if not value or not options:
        return None
    for alternative in value.split("|"):
        picked = _pick_one(alternative.strip(), options)
        if picked is not None:
            return picked
    return None


def pick_options(value: str, options: list[str]) -> str | None:
    """The options a multi-choice answer names, "Python | React", each item
    landed on its own option and kept in order; an item that lands nowhere
    is dropped. None when no item lands. Unlike pick_option, the items are
    all chosen, not alternatives tried in turn."""
    picked: list[str] = []
    for item in (value or "").split("|"):
        opt = _pick_one(item.strip(), options) if options else None
        if opt is not None and opt not in picked:
            picked.append(opt)
    return " | ".join(picked) or None


def _pick_one(value: str, options: list[str]) -> str | None:
    # Punctuation alone, a model's "[]", normalizes to nothing, and an empty
    # whole-word pattern would match the first option.
    if not normalize(value):
        return None
    low = value.lower()
    for opt in options:
        if opt.strip().lower() == low:
            return opt
    if YES.match(value) or NO.match(value):
        want = YES if YES.match(value) else NO
        for opt in options:
            if want.match(opt.strip()):
                return opt
        if want is NO:
            for opt in options:
                if NEGATED.search(normalize(opt)) and not DECLINED.search(opt):
                    return opt
        return None
    if DECLINED.search(value):
        for opt in options:
            if DECLINED.search(opt):
                return opt
        return None
    for opt in options:
        if opt.strip().lower().startswith(low):
            return opt
    words = re.compile(r"\b" + re.escape(normalize(value)) + r"\b")
    for opt in options:
        if words.search(normalize(opt)):
            return opt
    return None
