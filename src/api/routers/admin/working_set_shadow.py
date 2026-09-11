"""Read-only evidence for the user-job scope cutover."""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.auth import AuthedUser
from api.board import working_set_shadow
from api.routers.admin.shared import require_admin

router = APIRouter()


class SetComparison(BaseModel):
    old_total: int
    proposed_total: int
    both: int
    old_only: int
    proposed_only: int
    legacy_unknown: int
    old_only_known: int
    sample_old_only_known: list[str]
    sample_proposed_only: list[str]
    sample_legacy_unknown: list[str]


class DigestComparison(BaseModel):
    """Legacy non-force candidates and unclassifiable proposed rows.

    `cannot_tell` is the number of working-set rows with no admission time. It
    is not a proposed candidate count.
    """

    old_candidates: int
    proposed_candidates: int | None = None
    cannot_tell: int
    reason: str = working_set_shadow.DIGEST_UNKNOWN_REASON


class UserScopeComparison(BaseModel):
    user_id: int
    pair_membership: SetComparison | None
    stale_reverify_candidates: SetComparison | None
    digest_candidates: DigestComparison


class WorkingSetShadowReport(BaseModel):
    generated_at: datetime.datetime
    reverify_days: int
    reverify_per_cycle: int
    pair_membership: SetComparison
    ai_eligible_jobs: SetComparison
    stale_reverify_candidates: SetComparison
    scheduled_users: SetComparison
    digest_candidates: DigestComparison
    users: list[UserScopeComparison]


def _view(comparison: working_set_shadow.Comparison) -> SetComparison:
    return SetComparison(**vars(comparison))


@router.get("/working-set-shadow")
def working_set_shadow_report(
    user: AuthedUser = Depends(require_admin),
) -> WorkingSetShadowReport:
    """Old and proposed scope populations, without changing either one."""
    found = working_set_shadow.report()
    global_ = found.global_comparisons
    return WorkingSetShadowReport(
        generated_at=found.generated_at,
        reverify_days=found.reverify_days,
        reverify_per_cycle=found.reverify_per_cycle,
        pair_membership=_view(global_["pair_membership"]),
        ai_eligible_jobs=_view(global_["ai_eligible_jobs"]),
        stale_reverify_candidates=_view(global_["stale_reverify_candidates"]),
        scheduled_users=_view(global_["scheduled_users"]),
        digest_candidates=DigestComparison(
            old_candidates=sum(u.digest_old_candidates for u in found.users),
            cannot_tell=sum(u.digest_cannot_tell for u in found.users),
        ),
        users=[
            UserScopeComparison(
                user_id=u.user_id,
                pair_membership=_view(u.pair_membership) if u.pair_membership else None,
                stale_reverify_candidates=(
                    _view(u.stale_reverify_candidates) if u.stale_reverify_candidates else None
                ),
                digest_candidates=DigestComparison(
                    old_candidates=u.digest_old_candidates,
                    cannot_tell=u.digest_cannot_tell,
                ),
            )
            for u in found.users
        ],
    )
