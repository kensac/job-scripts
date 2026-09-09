from dataclasses import dataclass


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

    def metadata(self, total: int) -> dict[str, int | bool]:
        return {
            "page": self.number,
            "page_size": self.size,
            "total": total,
            "has_more": self.offset + self.size < total,
        }
