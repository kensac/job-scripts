from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PositiveInt,
    TypeAdapter,
)


class ColumnState(BaseModel):
    # Additional grid state survives writes, so new frontend columns and state
    # properties do not require a synchronized backend release.
    model_config = ConfigDict(extra="allow", strict=True)

    colId: str = Field(min_length=1)
    hide: bool | None = None
    pinned: Literal["left", "right"] | None = None
    width: PositiveInt | None = None


class SortKey(BaseModel):
    """One term of the board's ordering. `key` must be a column the board
    knows how to sort by; the board refuses an unknown one rather than
    ordering by something else, which is why this is validated here and not
    at the point it is read."""

    model_config = ConfigDict(strict=True)

    key: str = Field(min_length=1)
    dir: Literal["asc", "desc"] = "desc"


@dataclass(frozen=True)
class ConfigKey:
    default: JsonValue
    value_type: Any
    help: str
    # Which group of settings the admin page files this key under. The
    # registry is the only place that knows; a page grouping twenty keys
    # by guessing at their names guesses wrong the first time one is
    # renamed.
    section: str = "General"
    choices: tuple[str, ...] = ()
    kind: Literal["value", "text", "groups", "hosts", "columns"] = "value"
    _adapter: TypeAdapter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_adapter", TypeAdapter(self.value_type))
        self.validate(self.default)

    @property
    def type(self) -> type:
        return type(self.default)

    def validate(self, value: JsonValue) -> JsonValue:
        parsed = self._adapter.validate_python(value, strict=True)
        if self.choices and parsed not in self.choices:
            raise ValueError(f"takes one of {', '.join(self.choices)}")
        if self.kind == "groups" and any(not group.strip() for group in parsed):
            raise ValueError("takes nonempty group names")
        if self.kind == "hosts":
            normalized = {}
            for host, rate in parsed.items():
                name = host.strip().lower()
                if not name or name in normalized:
                    raise ValueError("takes distinct nonempty host names")
                normalized[name] = rate
            return normalized
        if self.kind == "columns":
            if len({column.colId for column in parsed}) != len(parsed):
                raise ValueError("takes distinct column identifiers")
            return [column.model_dump(exclude_unset=True) for column in parsed]
        return self._adapter.dump_python(parsed, mode="json")
