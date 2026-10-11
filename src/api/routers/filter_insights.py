"""Administrative and owner filter insights routers."""

from api.routers import filter_insights_admin, filter_insights_user

router = filter_insights_admin.router
user_router = filter_insights_user.user_router
