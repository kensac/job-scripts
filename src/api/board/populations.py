"""The labelled per-person populations that the legacy user_jobs name collapsed."""

from __future__ import annotations

from api.board import visibility
from api.board.person_state import PERSON_STATE

# The posting a person can see is FAST's answer less what they hid.
_VISIBLE = visibility.FAST.format(columns="count(*)", extra="AND NOT COALESCE(uj.hidden, FALSE)")


def per_user_counts(user: str) -> str:
    """The three labelled populations for one person, as select columns.

    acted_on: postings the person acted on (person state). working_set:
    postings the person's filters picked. visible: postings the person can
    see. `user` is an SQL expression for the user id (a column or a
    placeholder), never request text.
    """
    return f"""
        (SELECT count(*) FROM user_jobs uj WHERE uj.user_id = {user} AND {PERSON_STATE})
            AS acted_on,
        (SELECT count(*) FROM user_job_working_set ws WHERE ws.user_id = {user}) AS working_set,
        ({_VISIBLE.replace("%(uid)s", user)}) AS visible
    """
