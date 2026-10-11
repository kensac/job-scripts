"""Validate recipe JSON one byte at a time without retaining scalar values."""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from typing import BinaryIO


class InvalidRecipe(ValueError):
    pass


_SPACE = {9, 10, 13, 32}
_HEX = set(b"0123456789abcdefABCDEF")
_ESCAPES = {
    ord('"'): '"',
    ord("\\"): "\\",
    ord("/"): "/",
    ord("b"): "\b",
    ord("f"): "\f",
    ord("n"): "\n",
    ord("r"): "\r",
    ord("t"): "\t",
}


class Bytes:
    """A one-byte lookahead over a buffered file."""

    def __init__(self, stream: BinaryIO):
        self.stream = stream
        self.lookahead: int | None = None

    def peek(self) -> int | None:
        if self.lookahead is None:
            data = self.stream.read(1)
            self.lookahead = data[0] if data else -1
        return None if self.lookahead == -1 else self.lookahead

    def take(self) -> int:
        value = self.peek()
        if value is None:
            raise InvalidRecipe("recipe JSON ended early")
        self.lookahead = None
        return value

    def space(self) -> None:
        while self.peek() in _SPACE:
            self.take()

    def expect(self, value: int) -> None:
        if self.take() != value:
            raise InvalidRecipe("invalid recipe JSON syntax")

    def string(self, keep: int = 0) -> tuple[str, bool]:
        self.expect(ord('"'))
        saved: list[str] = []
        truncated = False
        decoder = codecs.getincrementaldecoder("utf-8")("strict")

        def capture(characters: str) -> None:
            nonlocal truncated
            for character in characters:
                if len(saved) < keep:
                    saved.append(character)
                else:
                    truncated = True

        while True:
            byte = self.take()
            if byte == ord('"'):
                try:
                    capture(decoder.decode(b"", final=True))
                except UnicodeDecodeError as exc:
                    raise InvalidRecipe("invalid UTF-8 in recipe JSON") from exc
                return "".join(saved), truncated
            if byte == ord("\\"):
                try:
                    capture(decoder.decode(b"", final=True))
                except UnicodeDecodeError as exc:
                    raise InvalidRecipe("invalid UTF-8 in recipe JSON") from exc
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                escape = self.take()
                if escape == ord("u"):
                    digits = bytes(self.take() for _ in range(4))
                    if any(digit not in _HEX for digit in digits):
                        raise InvalidRecipe("invalid Unicode escape in recipe JSON")
                    capture(chr(int(digits, 16)))
                elif escape in _ESCAPES:
                    capture(_ESCAPES[escape])
                else:
                    raise InvalidRecipe("invalid escape in recipe JSON")
            elif byte < 32:
                raise InvalidRecipe("control character in recipe JSON string")
            else:
                try:
                    capture(decoder.decode(bytes((byte,)), final=False))
                except UnicodeDecodeError as exc:
                    raise InvalidRecipe("invalid UTF-8 in recipe JSON") from exc

    def number(self) -> None:
        if self.peek() == ord("-"):
            self.take()
        digit = self.peek()
        if digit == ord("0"):
            self.take()
        elif digit is not None and ord("1") <= digit <= ord("9"):
            while (digit := self.peek()) is not None and ord("0") <= digit <= ord("9"):
                self.take()
        else:
            raise InvalidRecipe("invalid number in recipe JSON")
        if self.peek() == ord("."):
            self.take()
            self.digits()
        if self.peek() in (ord("e"), ord("E")):
            self.take()
            if self.peek() in (ord("+"), ord("-")):
                self.take()
            self.digits()

    def digits(self) -> None:
        first = self.peek()
        if first is None or not ord("0") <= first <= ord("9"):
            raise InvalidRecipe("invalid number in recipe JSON")
        while (digit := self.peek()) is not None and ord("0") <= digit <= ord("9"):
            self.take()


@dataclass
class Frame:
    kind: str
    scope: str
    state: str
    key: str = ""
    seen: set[str] = field(default_factory=set)
    name_valid: bool = False
    variants_valid: bool = False


def validate(stream: BinaryIO, adapter: str) -> None:
    """Check JSON syntax and the reader's required shape with bounded state."""
    source = Bytes(stream)
    source.space()
    source.expect(ord("{"))
    stack = [Frame("object", "root", "key_or_end")]
    root_name = False
    root_matches = False
    root_fields = False
    match_count = 0

    def value(parent: Frame) -> None:
        nonlocal root_name, root_matches, root_fields, match_count
        source.space()
        byte = source.peek()
        required: str | None = None
        if parent.scope == "root":
            required = {"name": "string", "matches": "array", "fields": "array"}.get(parent.key)
        elif parent.scope == "field":
            required = {"name": "string", "variants": "array"}.get(parent.key)
        elif parent.scope == "matches":
            required = "string"
        elif parent.scope == "fields":
            required = "object"
        kind = (
            "object"
            if byte == ord("{")
            else "array"
            if byte == ord("[")
            else "string"
            if byte == ord('"')
            else "number"
            if byte is not None and byte in b"-0123456789"
            else "literal"
        )
        if required is not None and kind != required:
            raise InvalidRecipe("recipe field has the wrong JSON type")
        if kind == "object":
            source.take()
            scope = "field" if parent.scope == "fields" else "other"
            stack.append(Frame("object", scope, "key_or_end"))
        elif kind == "array":
            source.take()
            scope = (
                parent.key
                if parent.scope == "root" and parent.key in ("matches", "fields")
                else "other"
            )
            stack.append(Frame("array", scope, "value_or_end"))
            if parent.scope == "root" and parent.key == "matches":
                root_matches = True
            elif parent.scope == "root" and parent.key == "fields":
                root_fields = True
            elif parent.scope == "field" and parent.key == "variants":
                parent.variants_valid = True
        elif kind == "string":
            keep = (
                len(adapter) + 1
                if parent.scope == "root" and parent.key == "name"
                else len("https://")
                if parent.scope == "matches"
                else 0
            )
            prefix, truncated = source.string(keep)
            if parent.scope == "root" and parent.key == "name":
                root_name = not truncated and prefix.lower() == adapter
            elif parent.scope == "field" and parent.key == "name":
                parent.name_valid = True
            elif parent.scope == "matches":
                if not prefix.startswith("https://"):
                    raise InvalidRecipe("matches must contain only https:// patterns")
                match_count += 1
        elif kind == "number":
            source.number()
        elif byte in (ord("t"), ord("f"), ord("n")):
            literal = {ord("t"): b"true", ord("f"): b"false", ord("n"): b"null"}[byte]
            for character in literal:
                source.expect(character)
        else:
            raise InvalidRecipe("invalid recipe JSON value")

    while stack:
        current = stack[-1]
        source.space()
        byte = source.peek()
        if current.kind == "object":
            if current.state in ("key_or_end", "key"):
                if byte == ord("}") and current.state == "key_or_end":
                    source.take()
                    if current.scope == "field" and not (
                        current.name_valid and current.variants_valid
                    ):
                        raise InvalidRecipe("each field needs a name and variants list")
                    stack.pop()
                    continue
                key, truncated = source.string(len("variants"))
                current.key = key if not truncated else ""
                relevant = (
                    {"name", "matches", "fields"}
                    if current.scope == "root"
                    else {"name", "variants"}
                    if current.scope == "field"
                    else set()
                )
                if current.key in relevant:
                    if current.key in current.seen:
                        raise InvalidRecipe("duplicate recipe field")
                    current.seen.add(current.key)
                source.space()
                source.expect(ord(":"))
                current.state = "value"
            elif current.state == "value":
                current.state = "comma_or_end"
                value(current)
            else:
                if byte == ord(","):
                    source.take()
                    current.state = "key"
                elif byte == ord("}"):
                    source.take()
                    if current.scope == "field" and not (
                        current.name_valid and current.variants_valid
                    ):
                        raise InvalidRecipe("each field needs a name and variants list")
                    stack.pop()
                else:
                    raise InvalidRecipe("invalid recipe JSON object")
        elif current.state in ("value_or_end", "value"):
            if byte == ord("]") and current.state == "value_or_end":
                source.take()
                stack.pop()
            else:
                current.state = "comma_or_end"
                value(current)
        elif byte == ord(","):
            source.take()
            current.state = "value"
        elif byte == ord("]"):
            source.take()
            stack.pop()
        else:
            raise InvalidRecipe("invalid recipe JSON array")
    source.space()
    if source.peek() is not None:
        raise InvalidRecipe("trailing data after recipe JSON")
    if not (root_name and root_matches and root_fields and match_count):
        raise InvalidRecipe("recipe is missing a valid name, matches, or fields")
