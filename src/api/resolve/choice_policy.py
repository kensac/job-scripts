from __future__ import annotations

from typing import Any

from api import db
from api.mail import match as mail_match
from api.resolve.contracts import ASSIGN, NOT_AN_APPLICATION, NOT_JOB_RELATED


def _choice(
    choice: str,
    label: str,
    *,
    eligible: bool = True,
    reason: str | None = None,
    messages: int = 1,
    target_source: str | None = None,
) -> dict[str, Any]:
    """One verb, as the picker will render it.

    `reason` is PRINTED next to a refused verb rather than hidden behind a
    hover, so it has to be a short clause that survives being typeset inline.

    `affects` is omitted when the verb touches exactly one message, and
    omission MEANS one - never "unknown". A verb that can reach further and
    drops the field would wear a single-message costume, so a producer that
    cannot count precisely sends its best truth instead of nothing.
    """
    out: dict[str, Any] = {"choice": choice, "label": label, "eligible": eligible}
    if reason:
        out["reason"] = reason
    if messages > 1:
        out["affects"] = {"messages": messages}
    if target_source:
        # The two travel together deliberately. A verb that says it needs a
        # target without saying where the options come from has moved the
        # guess rather than removed it.
        out["needs_target"] = True
        out["target_source"] = target_source
    return out


def _thread_sizes(user_id: int) -> dict[str, int]:
    """How many messages each conversation holds, in one query.

    Asked per message it was one round trip per row, and the queue ranks
    before it pages - so a fifty-row page cost a query for every one of the
    3,251 rows behind it. The count is a property of the thread, not of the
    message, so it is one GROUP BY.
    """
    return {
        row["provider_thread_id"]: int(row["c"])
        for row in db.query(
            "SELECT provider_thread_id, count(*) AS c FROM email_messages "
            "WHERE user_id = %s AND provider_thread_id IS NOT NULL "
            "GROUP BY provider_thread_id",
            (user_id,),
        )
    }


def thread_size(user_id: int, thread: str | None) -> int:
    """How many messages one assign would actually move.

    Assign carries the whole conversation by default, so the count belongs in
    the button rather than in the response afterwards - a person deciding one
    message should know before clicking that it moves fourteen. For a whole
    queue page use `_thread_sizes`, which asks once instead of once per row.
    """
    if not thread:
        return 1
    row = db.query_one(
        "SELECT count(*) AS c FROM email_messages WHERE user_id = %s AND provider_thread_id = %s",
        (user_id, thread),
    )
    return max(1, int((row or {}).get("c", 1)))


def by_company(apps: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Applications indexed by normalised company, built once per request.

    The queue ranks before it pages, so every helper below ran over the whole
    application list for every one of 3,251 rows - 8.2 million `norm_company`
    calls, each two regex substitutions, to answer a question that has 2,543
    distinct answers. Normalising each side once is the same predicate, and it
    is the ONLY place the two sides may be compared: `norm_company` is what
    makes "Stripe" and "Stripe, Inc." one employer, and an index keyed on raw
    text would silently be a stricter matcher than the one it stands in for.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for app in apps:
        key = mail_match.norm_company(app["company_name"])
        if key:
            index.setdefault(key, []).append(app)
    return index


def choices_for_message(
    apps_by_company: dict[str, list[dict[str, Any]]],
    company: str | None,
    thread_size: int,
    target_source: str,
) -> list[dict[str, Any]]:
    """The verbs available on one message, decided HERE rather than by the
    caller.

    Shared with the candidate picker, which is the surface a person actually
    makes this decision on. A modal that builds the verb list itself has to
    decide eligibility client-side, and eligibility decided client-side is
    exactly what a server-declared contract exists to prevent - the first time
    a verb becomes conditional, one of the two lists is wrong and nothing says
    which.

    Takes the index, the thread size and the name of its own target list rather
    than fetching or assuming them. Required, not defaulted: a default is how
    "I did not have this" hides inside shared code as if it were "there is
    nothing", and all three are already in hand at every call site.

    `target_source` IS A PER-SURFACE FACT and that is why the caller states it.
    Two surfaces share these verbs and they hold their applications under
    different keys - the queue row calls the list `candidates`, the picker
    calls it `applications`. A constant here would name whichever one was
    written first and be wrong on the other, which is the same hardcoded fact
    the field exists to remove, moved one module along.

    ELIGIBILITY IS "AN APPLICATION EXISTS AT THIS COMPANY", not "the list in
    front of you is non-empty". On the queue those coincide, because the row's
    candidates and this index are the same set read under the same key. On the
    picker they do not: its list is search-filtered, and typing a query that
    matches nothing does not stop the application existing. So the caller that
    wants the stronger claim - eligible exactly when its own list is non-empty
    - is the queue, and it holds there by construction rather than by promise.
    """
    key = mail_match.norm_company(company)
    if key and apps_by_company.get(key):
        assign = _choice(
            ASSIGN,
            "Belongs to an application",
            messages=thread_size,
            target_source=target_source,
        )
    else:
        assign = _choice(
            ASSIGN,
            "Belongs to an application",
            eligible=False,
            reason="no application at this company yet",
            target_source=target_source,
        )
    return [
        assign,
        _choice(NOT_AN_APPLICATION, "Belongs to no application"),
        _choice(NOT_JOB_RELATED, "Not job mail"),
    ]
