"""Which models' verdicts count as one model's own.

A verdict is keyed by the model that gave it, so a model switch re-pays for
every posting. A group here lets a move between measured models keep the
answers already held. It changes reads only: every verdict still records the
model that gave it, and an empty list is the keyed-by-model behavior exactly,
so clearing the setting reverts the change.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from core import providers


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # custom: filter and managed-board verdicts. verify: closed and clearance
    # verdicts, through re-verification's unchanged-page reuse.
    kind: Literal["custom", "verify"]
    models: Annotated[list[str], Field(min_length=2)]


class ModelEquivalence(RootModel[list[Group]]):
    root: list[Group] = []

    @model_validator(mode="after")
    def _known_and_disjoint(self) -> ModelEquivalence:
        seen: set[tuple[str, str]] = set()
        for group in self.root:
            if len(set(group.models)) != len(group.models):
                raise ValueError(f"{group.kind}: a group repeats a model")
            for name in group.models:
                if name not in providers.MODELS:
                    raise ValueError(f"{group.kind}: unknown model {name!r}")
                if (group.kind, name) in seen:
                    raise ValueError(f"{group.kind}: {name!r} is in two groups")
                seen.add((group.kind, name))
        return self

    def answering(self, kind: str, model: str) -> list[str]:
        """`model` and every model whose `kind` verdict counts as its own."""
        for group in self.root:
            if group.kind == kind and model in group.models:
                return sorted(group.models)
        return [model]
