"""The provider registry.

One place the router, the pricer and the settings UI all read from, so that
adding a provider is adding a module here rather than editing a catalogue, a
price table, an effort union and a key-env map in four files that can disagree.
"""

from __future__ import annotations

from core.providers import anthropic, openai
from core.providers.spec import (
    Model,
    Output,
    Provider,
    Rates,
    Reasoning,
    Source,
    StructuredOutput,
    StructuredOutputSpec,
    Tier,
    Wire,
)

__all__ = [
    "MODELS",
    "PROVIDERS",
    "Model",
    "Output",
    "Provider",
    "Rates",
    "Reasoning",
    "Source",
    "StructuredOutput",
    "StructuredOutputSpec",
    "Tier",
    "Wire",
    "model",
    "provider_of",
]

# Declaration order is the order the UI offers them in.
_MODULES = (openai, anthropic)

PROVIDERS: dict[str, Provider] = {m.PROVIDER.name: m.PROVIDER for m in _MODULES}


def _index() -> dict[str, tuple[str, Model]]:
    """model name -> (provider name, model).

    A model name has to be globally unique, because it is the only key the
    stored settings, the usage rows and the price lookup carry - none of them
    records which provider served the call. Two providers publishing the same
    name would silently cross-price, so this refuses to build rather than
    resolving it arbitrarily.
    """
    out: dict[str, tuple[str, Model]] = {}
    for provider in PROVIDERS.values():
        for m in provider.models:
            if m.name in out:
                raise ValueError(
                    f"model {m.name!r} is declared by both {out[m.name][0]} and {provider.name}"
                )
            out[m.name] = (provider.name, m)
    return out


MODELS = _index()


def model(name: str | None) -> Model | None:
    entry = MODELS.get(name or "")
    return entry[1] if entry else None


def provider_of(name: str | None) -> str | None:
    entry = MODELS.get(name or "")
    return entry[0] if entry else None
