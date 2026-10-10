from api import budget
from api.auth import AuthedUser
from api.problem import refuse


def require_config(user: AuthedUser):
    try:
        return budget.resolve_ai_config(user.id, budget.get_entitlement(user))
    except budget.AIAccessError as exc:
        raise refuse(402, exc.reason, exc.message) from exc
