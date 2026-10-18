from api import ai_pause, budget
from api.auth import AuthedUser
from api.problem import refuse

PAUSED_MESSAGE = "AI features are paused by an administrator. Everything else still works."


def require_config(user: AuthedUser):
    # Before the budget, so a paused site says so instead of naming a key.
    if ai_pause.paused():
        raise refuse(503, "AI_PAUSED", PAUSED_MESSAGE)
    try:
        return budget.resolve_ai_config(user.id, budget.get_entitlement(user))
    except budget.AIAccessError as exc:
        raise refuse(402, exc.reason, exc.message) from exc
