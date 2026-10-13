"""Shared spend windows and legacy transport classification."""

from api import model_calls

# Legacy batching diagnostics classify contexts, not verified transport history.
INTERACTIVE_CONTEXTS = ("explain", "manual")

_WINDOW_START = "now() - make_interval(days => %(days)s)"
_WINDOW = f"created_at >= {_WINDOW_START}"
# The window's answers, each with its call's usage (model_calls.answers_with_usage).
_ANSWERS = model_calls.answers_with_usage(f"q.{_WINDOW} AND q.model IS NOT NULL")
