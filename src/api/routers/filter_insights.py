"""Administrative and owner filter insights routers."""

from api.routers.filtering import admin_insights, person_insights

router = admin_insights.router
user_router = person_insights.user_router
