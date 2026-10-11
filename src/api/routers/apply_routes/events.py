"""Privacy-scoped events for assisted application actions."""

from typing import Any

from api import telemetry
from api.auth import AuthedUser


def record_apply_event(event: str, user: AuthedUser, **properties: Any) -> None:
    """One event per thing the extension did, keyed to the person.

    distinct_id is the IdP subject, which is what the API already uses for
    exceptions (app.py) and what the frontend identifies with: auth.js mints a
    fresh token.sub at every sign-in, so it surfaces the stable idp_sub to
    PostHog instead. Anything else here would put the extension's funnel on a
    different person from the same person's web session.

    Properties carry counts and the ATS host, never a field label, a question
    or an answer. What someone was asked and what they replied is the content
    of their application; the shape of it is what tells us whether a reader
    works.
    """
    telemetry.capture(event, distinct_id=user.sub, properties=properties)
