"""What an answer was asked, rebuilt from the page fetch it judged.

An answer in ai_queries kept a copy of its input (input_content). The input
is a function of the page text and columns the answer already has, fixed by
the code path that wrote it (its config_name):

- a custom filter's answer wraps the page with the company and title
  (core.filters.build_custom_input); verification answering a board's
  question in the same request read the page cut to VERIFY_INPUT_CHARS;
- an admin re-check or a person's explain read the page cut to
  LIVE_CHECK_INPUT_CHARS, unwrapped;
- a closed or clearance answer read the page as it was.

`sql` is that function in SQL, so an answer that points at its fetch
(page_fetch_id) carries no copy. tasks.answer_links points an older answer at
a fetch only where this rebuilds its copy byte for byte; where no fetch held
the text, it stores the text the answer saw as a fetch first.
"""

from __future__ import annotations

from core.answers import VERIFY_INPUT_CHARS

# A live check reads at most this much of the page: an admin re-check
# (routers/admin/checks.py) and a person's explain (routers/job_explain.py).
LIVE_CHECK_INPUT_CHARS = 60000

# The contexts whose answers read the page unwrapped and cut to
# LIVE_CHECK_INPUT_CHARS, whatever the check.
_LIVE = ("manual", "explain")

# What build_custom_input puts before the page, from the answer's columns.
_HEADER = (
    "'Company: ' || COALESCE({q}.company, '') || E'\\nJob Title: ' || "
    "COALESCE({q}.job_title, '') || E'\\n\\nJob Content:\\n'"
)


def wrapped(q: str) -> str:
    """True when the answer `q` wrapped its page with a header."""
    live = ", ".join(f"'{c}'" for c in _LIVE)
    return f"({q}.check_type = 'custom' AND COALESCE({q}.config_name, '') NOT IN ({live}))"


def header(q: str) -> str:
    return "(" + _HEADER.format(q=q) + ")"


def _cut(q: str, text: str) -> str:
    live = ", ".join(f"'{c}'" for c in _LIVE)
    return (
        f"left({text}, CASE WHEN COALESCE({q}.config_name, '') IN ({live}) "
        f"THEN {LIVE_CHECK_INPUT_CHARS} "
        f"WHEN {wrapped(q)} AND {q}.config_name = 'verify-batch' THEN {VERIFY_INPUT_CHARS} "
        "ELSE 2147483647 END)"
    )


def sql(q: str, text: str) -> str:
    """The input of answer `q` (an ai_queries alias) rebuilt from page text
    `text` (an expression, usually a page_fetches alias's content)."""
    return f"(CASE WHEN {wrapped(q)} THEN {header(q)} || {_cut(q, text)} ELSE {_cut(q, text)} END)"


def seen(q: str) -> str:
    """The page text answer `q` saw, cut out of its stored copy: the copy
    without its header. Only meaningful where the header is the one `q`'s
    columns rebuild (`header_matches`)."""
    return (
        f"(CASE WHEN {wrapped(q)} THEN substr({q}.input_content, length({header(q)}) + 1) "
        f"ELSE {q}.input_content END)"
    )


def header_matches(q: str) -> str:
    """The stored copy starts with the header `q`'s columns rebuild, or `q`
    wraps nothing."""
    return f"(NOT {wrapped(q)} OR left({q}.input_content, length({header(q)})) = {header(q)})"
