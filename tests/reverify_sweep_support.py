"""Database-clock helpers shared by re-verification behavior tests."""

from api import db


def age_closed_verdict(url: str, days: int) -> None:
    """Move a verdict back on the DATABASE's clock. A verdict aged from Python
    is compared against now() by the staleness predicate, which is two clocks."""
    db.execute(
        "UPDATE ai_queries SET created_at = now() - make_interval(days => %s) "
        "WHERE url = %s AND check_type = 'closed'",
        (days, url),
    )
