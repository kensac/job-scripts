"""Vendor batch availability and this application's implemented transports."""

from core import providers


def vendor_support(model: str) -> bool | None:
    declared = providers.model(model)
    provider = providers.provider_of(model)
    if declared is None or provider is None:
        return None
    if declared.batch_supported is not None:
        return declared.batch_supported
    if provider == "openai":
        return providers.PROVIDERS[provider].batch_endpoint is not None
    return None


def unavailable_reason(model: str) -> str | None:
    provider = providers.provider_of(model)
    if provider is None:
        return "model is not declared"
    if providers.PROVIDERS[provider].batch_endpoint is None:
        return "provider has no batch endpoint"
    supported = vendor_support(model)
    if supported is False:
        return "provider does not support batching this model"
    if supported is None:
        return "batch support for this model is unverified"
    # The persisted collector authenticates with the OpenAI server key and
    # consumes Responses/Embeddings receipts. A compatible live API does not
    # make another vendor's batch protocol or credentials interchangeable.
    if provider != "openai":
        return "provider batch transport is not implemented in this application"
    return None
