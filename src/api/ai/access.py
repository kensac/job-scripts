from fastapi import HTTPException

from api import budget
from api.auth import AuthedUser


def require_config(user: AuthedUser):
    try:
        return budget.resolve_ai_config(user.id, budget.get_entitlement(user))
    except budget.AIAccessError as exc:
        raise HTTPException(402, detail={"code": exc.reason, "message": exc.message}) from exc
