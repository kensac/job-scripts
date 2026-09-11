from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel

ASSIGN = "assign_application"
NOT_AN_APPLICATION = "not_an_application"
NOT_JOB_RELATED = "not_job_related"
CONFIRM_MATCH = "confirm_match"
REJECT_MATCH = "reject_match"
ACCEPT_STATUS = "accept_status"
DECLINE_STATUS = "decline_status"
MARK_DONE = "mark_done"

# Where a verb's target comes from: the name of the field, ON THIS RESPONSE,
# holding the options. Not a global constant - the two surfaces that offer
# these verbs hold their applications under different keys, so each declares
# its own and a client reads `payload[choice.target_source]` without knowing
# which surface it is on.
CANDIDATES = "candidates"
PICKER_APPLICATIONS = "applications"

UNMATCHED_MESSAGE = "unmatched_message"
UNCONFIRMED_MATCH = "unconfirmed_match"
STATUS_PROPOSAL = "status_proposal"
ACTION_ITEM = "action_item"

# Every kind the queue can hold, in the order a caller sees them declared.
# Exposed as a filter value rather than as knowledge the client has to carry,
# so a fifth kind is a server change alone.
ITEM_KINDS = (UNMATCHED_MESSAGE, UNCONFIRMED_MATCH, STATUS_PROPOSAL, ACTION_ITEM)


# DECLARED RESPONSE SCHEMAS, which is not the house style yet and should be.
# 123 of the 128 operations in openapi.json ship `"schema": {}`, so a client
# and this server can disagree about an envelope and nothing mechanical
# notices - four such mismatches were found by hand in one day, every one
# silent. A generated schema is the only place that drift becomes detectable,
# and the newest surface is the cheapest place to start rather than a
# retrofit.
class ResolveChoiceAffects(BaseModel):
    messages: int


# exclude_none on the routes below is load-bearing, not tidiness. `affects`
# omitted means one message and `reason` omitted means the verb is available;
# serialising either as an explicit null would say something the contract does
# not - and the picker reads presence, not value.
class ResolveChoice(BaseModel):
    choice: str
    label: str
    eligible: bool
    reason: str | None = None
    # Omitted when the verb touches exactly one message, and omission MEANS
    # one rather than unknown.
    affects: ResolveChoiceAffects | None = None
    # Whether pressing this verb can be POSTed straight away or has to collect
    # a target first. Omitted means it takes no target.
    #
    # Without it a client has to know that `assign_application` is the verb
    # with an argument, which was the last piece of this vocabulary it was
    # still required to hardcode - and the point of declaring the verbs is that
    # a new decision type needs no client change. A verb that takes an argument
    # and cannot say so makes every client wrong the first time there are two
    # of them.
    needs_target: bool | None = None
    # WHERE the options come from, named rather than assumed. Every target
    # comes from the row's own `candidates` today; a verb that picked from
    # somewhere else would otherwise be a second silent assumption stacked on
    # the first.
    target_source: str | None = None


class ResolveCandidate(BaseModel):
    id: int
    company_name: str | None = None
    title: str | None = None
    applied_at: datetime.datetime | None = None


class ResolveMessage(BaseModel):
    id: int
    subject: str | None = None
    from_email: str | None = None
    sent_at: datetime.datetime | None = None
    classified_as: str | None = None
    extracted_company: str | None = None
    extracted_title: str | None = None


class ResolveApplication(BaseModel):
    """The application a row is about, with the stage the board would show.

    `stage` is `mail_pipeline.stage_for` over that application's real events,
    not a second reading of them, so the queue and the board cannot disagree
    about what an application is doing while asking about it.
    """

    id: int
    company_name: str | None = None
    title: str | None = None
    stage: str | None = None
    on_board: bool = False
    # The posting on the board, when there is one. Omitted means the
    # application has no posting - mail predating the catalog is the normal
    # case - and a client that wants to open the posting reads presence.
    job_id: int | None = None


class ResolveImplication(BaseModel):
    """What answering would change beyond the row itself.

    Present only where there is something to say. A control whose effect
    reaches past the row states that before the click rather than reporting it
    afterwards, and `board_updated` is the honest half of that: an application
    with no board row has a status to propose and nothing to move.
    """

    board_status: str
    from_status: str | None = None
    board_updated: bool
    reason: str | None = None


class ResolveMatch(BaseModel):
    id: int
    method: str
    confidence: str | None = None
    rationale: str | None = None
    created_at: datetime.datetime | None = None


class ResolveAction(BaseModel):
    id: int
    kind: str
    due_at: datetime.datetime | None = None
    # What could ever close this without a person. Empty means nothing can,
    # which is why the item is here rather than waiting on the next email.
    settles_on: list[str]


class ResolveItem(BaseModel):
    id: str
    kind: str
    rank: int
    rank_reason: str
    choices: list[ResolveChoice]
    message: ResolveMessage | None = None
    candidates: list[ResolveCandidate] | None = None
    application: ResolveApplication | None = None
    implies: ResolveImplication | None = None
    match: ResolveMatch | None = None
    action: ResolveAction | None = None


class ResolveQueue(BaseModel):
    items: list[ResolveItem]
    total: int
    # How many sit at each rank, so a page can say "40 need you, 3,623 do
    # not" rather than implying the first fifty are all there is.
    by_rank: dict[str, int]
    # The same honesty per kind, which is what makes the one queue readable as
    # the four questions it merges rather than as an undifferentiated pile.
    by_kind: dict[str, int]


class ResolveResult(BaseModel):
    ok: bool
    choice: str
    application_id: int | None = None
    # What the answer actually touched. Omitted where the verb touches nothing
    # beyond the row, present and false where it was meant to and could not.
    board_updated: bool | None = None
    board_status: str | None = None
    reason: str | None = None


class ResolveRequest(BaseModel):
    choice: Literal[
        "assign_application",
        "not_an_application",
        "not_job_related",
        "confirm_match",
        "reject_match",
        "accept_status",
        "decline_status",
        "mark_done",
    ]
    target: int | None = None
    note: str | None = None


class DecisionRow(BaseModel):
    id: str
    at: datetime.datetime
    kind: str
    decision: str
    by: str
    summary: str
    application_id: int | None = None
    # The decision this one replaced, and whether something later replaced
    # THIS one. An overturned answer that vanishes takes the evidence that the
    # rule was wrong with it.
    superseded_by: str | None = None
    supersedes: str | None = None


class DecisionHistory(BaseModel):
    decisions: list[DecisionRow]
    total: int


class ReviewRate(BaseModel):
    method: str
    confidence: str | None = None
    attached: int
    reviewed: int
    confirmed: int
    rejected: int
    # NULL, not zero. A tier nobody has reviewed has no rate, and rendering
    # that as 0% says the tier is always wrong.
    confirm_rate: float | None = None
    note: str | None = None


class ReviewRates(BaseModel):
    by_method: list[ReviewRate]
    never_reviewed: int
    reviewed: int
