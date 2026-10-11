"""Administrative people, invitation, and group budget routes."""

from fastapi import APIRouter

from api.routers.admin import people_budgets, people_invites, people_users

router = APIRouter()
router.include_router(people_users.router)
router.include_router(people_invites.router)
router.include_router(people_budgets.router)
