"""Public configuration read by the browser extension."""

from fastapi import APIRouter, Query, Response

from api import db
from api.apply import policy as extension_policy
from api.apply import recipes as extension_recipes
from api.problem import refuse

router = APIRouter()


@router.get("/extension/config", response_model=extension_policy.ExtensionConfig)
def extension_config(
    response: Response,
    adapter: extension_policy.AdapterId,
    schema_version: int = Query(ge=1),
):
    # Disablements must remain readable when the person's session expires.
    # This public response contains no profile, answers or credentials. The
    # extension alone owns the bounded cache and pins a revision per fill.
    response.headers["Cache-Control"] = "no-store"
    if schema_version != 1:
        raise refuse(409, "UNSUPPORTED_CONFIG_SCHEMA", "this configuration schema is not supported")
    try:
        policy = extension_policy.ExtensionPolicy.model_validate(db.get_config("extension_policy"))
    except ValueError as exc:
        raise refuse(
            503, "INVALID_EXTENSION_POLICY", "extension configuration is unavailable"
        ) from exc
    return extension_policy.resolve(policy, adapter)


@router.get("/extension/recipe", response_model=extension_recipes.RecipeConfig)
def extension_recipe(
    response: Response,
    adapter: extension_policy.AdapterId,
    schema_version: int = Query(ge=1),
):
    """The published table for a config-driven reader, or 404 when only the
    bundled copy exists. Same public terms as /extension/config: no profile,
    answers or credentials; the extension pins a revision per fill and keeps
    the bundled table as its fallback (api.apply.recipes)."""
    response.headers["Cache-Control"] = "no-store"
    if schema_version != 1:
        raise refuse(409, "UNSUPPORTED_CONFIG_SCHEMA", "this configuration schema is not supported")
    try:
        policy = extension_policy.ExtensionPolicy.model_validate(db.get_config("extension_policy"))
    except ValueError as exc:
        raise refuse(
            503, "INVALID_EXTENSION_POLICY", "extension configuration is unavailable"
        ) from exc
    config = extension_recipes.config_for(adapter, policy.max_age_seconds)
    if config is None:
        raise refuse(
            404, "NO_RECIPE", "no published recipe for this adapter; the bundled table applies"
        )
    return config
