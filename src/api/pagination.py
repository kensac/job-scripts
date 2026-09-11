from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Page:
    number: int
    size: int

    @classmethod
    def from_params(cls, number: int, size: int, *, maximum: int) -> "Page":
        return cls(max(1, number), max(1, min(size, maximum)))

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    def metadata(self, total: int) -> dict[str, Any]:
        """The four fields every paged envelope carries. `Any` rather than
        `int | bool` so a caller can unpack this into a declared response
        model: the union assigns to neither field."""
        return {
            "page": self.number,
            "page_size": self.size,
            "total": total,
            "has_more": self.offset + self.size < total,
        }
