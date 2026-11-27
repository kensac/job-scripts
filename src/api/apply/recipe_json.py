"""Validate recipe JSON while retaining only the current chunk and parser state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field


class InvalidRecipe(ValueError):
    pass


_SPACE = set(b" \t\n\r")
_HEX = set(b"0123456789abcdefABCDEF")
_DIGITS = set(b"0123456789")
_NONZERO_DIGITS = set(b"123456789")
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
_LITERALS = {ord("t"): "true", ord("f"): "false", ord("n"): "null"}
_OPEN_OBJECT = ord("{")
_CLOSE_OBJECT = ord("}")
_OPEN_ARRAY = ord("[")
_CLOSE_ARRAY = ord("]")
_QUOTE = ord('"')
_BACKSLASH = ord("\\")
_COLON = ord(":")
_COMMA = ord(",")
_MINUS = ord("-")
_PLUS = ord("+")
_ZERO = ord("0")
_DOT = ord(".")
_EXPONENT = set(b"eE")
_UNICODE_ESCAPE = ord("u")
_FIRST_PRINTABLE = ord(" ")

_COMPLETE_NUMBER_STATES = frozenset({"zero", "int", "fraction", "exp_digits"})

# The keys a recipe must give, and the JSON type each must have, by the
# object that holds them.
_REQUIRED_KEYS = {
    "root": {"name": "string", "matches": "array", "fields": "array"},
    "field": {"name": "string", "variants": "array"},
}

_CHECKPOINT_DEFAULTS: dict[str, object] = {
    "depth": 1,
    "mode": "root",
    "root_name": False,
    "root_matches": False,
    "root_fields": False,
    "match_count": 0,
    "string_context": "",
    "prefix": "",
    "truncated": False,
    "utf8_left": 0,
    "utf8_value": 0,
    "utf8_min": 0,
    "unicode_digits": "",
    "number_state": "",
    "literal": "",
    "literal_pos": 0,
}


def _value_kind(byte: int) -> str:
    if byte == _OPEN_OBJECT:
        return "object"
    if byte == _OPEN_ARRAY:
        return "array"
    if byte == _QUOTE:
        return "string"
    if byte == _MINUS or byte in _DIGITS:
        return "number"
    return "literal"


_NUMBER_TRANSITIONS: dict[str, dict[str, str]] = {
    "start": {"minus": "minus", "zero": "zero", "nonzero": "int"},
    "minus": {"zero": "zero", "nonzero": "int"},
    "zero": {"dot": "dot", "exp": "exp"},
    "int": {"zero": "int", "nonzero": "int", "dot": "dot", "exp": "exp"},
    "dot": {"zero": "fraction", "nonzero": "fraction"},
    "fraction": {"zero": "fraction", "nonzero": "fraction", "exp": "exp"},
    "exp": {
        "plus": "exp_sign",
        "minus": "exp_sign",
        "zero": "exp_digits",
        "nonzero": "exp_digits",
    },
    "exp_sign": {"zero": "exp_digits", "nonzero": "exp_digits"},
    "exp_digits": {"zero": "exp_digits", "nonzero": "exp_digits"},
}


def _number_token(byte: int) -> str:
    if byte == _MINUS:
        return "minus"
    if byte == _PLUS:
        return "plus"
    if byte == _ZERO:
        return "zero"
    if byte in _NONZERO_DIGITS:
        return "nonzero"
    if byte == _DOT:
        return "dot"
    if byte in _EXPONENT:
        return "exp"
    return "other"


def _next_number_state(state: str, byte: int) -> str | None:
    """Return the next number state, None when a complete number ends on a delimiter,
    or raise InvalidRecipe on an illegal byte."""
    token = _number_token(byte)
    next_state = _NUMBER_TRANSITIONS.get(state, {}).get(token)
    if next_state is not None:
        return next_state
    if state in _COMPLETE_NUMBER_STATES and not (state == "zero" and token in ("zero", "nonzero")):
        return None
    raise InvalidRecipe("invalid number in recipe JSON")


@dataclass
class Frame:
    kind: str
    scope: str
    state: str
    key: str = ""
    seen: list[str] = field(default_factory=list)
    name_valid: bool = False
    variants_valid: bool = False


class RecipeParser:
    """A resumable byte parser. One feed accepts one stored upload chunk."""

    depth: int
    mode: str
    root_name: bool
    root_matches: bool
    root_fields: bool
    match_count: int
    string_context: str
    prefix: str
    truncated: bool
    utf8_left: int
    utf8_value: int
    utf8_min: int
    unicode_digits: str
    number_state: str
    literal: str
    literal_pos: int

    def __init__(
        self,
        adapter: str,
        checkpoint: dict | None,
        load_frame: Callable[[int], dict | None],
    ) -> None:
        self.adapter = adapter
        self.load_frame = load_frame
        self.frames: dict[int, Frame] = {}
        self.deleted_from: int | None = None
        state = _CHECKPOINT_DEFAULTS if checkpoint is None else checkpoint
        for key, value in state.items():
            setattr(self, key, value)
        if checkpoint is None:
            self.frames[0] = Frame("object", "root", "key_or_end")

    def frame(self, depth: int | None = None) -> Frame:
        index = self.depth - 1 if depth is None else depth
        if index not in self.frames:
            raw = self.load_frame(index)
            if raw is None:
                raise InvalidRecipe("recipe parser state is missing")
            self.frames[index] = Frame(**raw)
        return self.frames[index]

    def push(self, frame: Frame) -> None:
        self.frames[self.depth] = frame
        self.depth += 1

    def pop(self) -> None:
        current = self.frame()
        if current.scope == "field" and not (current.name_valid and current.variants_valid):
            raise InvalidRecipe("each field needs a name and variants list")
        self.depth -= 1
        self.deleted_from = (
            self.depth if self.deleted_from is None else min(self.deleted_from, self.depth)
        )

    def required_kind(self, frame: Frame) -> str | None:
        if frame.scope in _REQUIRED_KEYS:
            return _REQUIRED_KEYS[frame.scope].get(frame.key)
        if frame.scope == "matches":
            return "string"
        if frame.scope == "fields":
            return "object"
        return None

    def start_value(self, byte: int) -> None:
        parent = self.frame()
        kind = _value_kind(byte)
        required = self.required_kind(parent)
        if required is not None and required != kind:
            raise InvalidRecipe("recipe field has the wrong JSON type")
        parent.state = "comma_or_end"
        if kind == "object":
            self.push(
                Frame("object", "field" if parent.scope == "fields" else "other", "key_or_end")
            )
        elif kind == "array":
            self._start_array(parent)
        elif kind == "string":
            self.start_string("value")
        elif kind == "number":
            self.mode = "number"
            self.number_state = "start"
            self.number(byte)
        elif byte in _LITERALS:
            self.mode = "literal"
            self.literal = _LITERALS[byte]
            self.literal_pos = 1
        else:
            raise InvalidRecipe("invalid recipe JSON value")

    def _start_array(self, parent: Frame) -> None:
        scope = (
            parent.key
            if parent.scope == "root" and parent.key in ("matches", "fields")
            else "other"
        )
        self.push(Frame("array", scope, "value_or_end"))
        if parent.scope == "root" and parent.key == "matches":
            self.root_matches = True
        elif parent.scope == "root" and parent.key == "fields":
            self.root_fields = True
        elif parent.scope == "field" and parent.key == "variants":
            parent.variants_valid = True

    def start_string(self, role: str) -> None:
        self.mode = "string"
        self.string_context = role
        self.prefix = ""
        self.truncated = False
        self.utf8_left = 0
        self.utf8_value = 0
        self.utf8_min = 0

    def capture(self, character: str) -> None:
        frame = self.frame()
        keep = (
            len("variants")
            if self.string_context == "key"
            else len(self.adapter) + 1
            if frame.scope == "root" and frame.key == "name"
            else len("https://")
            if frame.scope == "matches"
            else 0
        )
        if len(self.prefix) < keep:
            self.prefix += character
        else:
            self.truncated = True

    def _decode_utf8_continuation(self, byte: int) -> None:
        if byte & 0xC0 != 0x80:
            raise InvalidRecipe("invalid UTF-8 in recipe JSON")
        self.utf8_value = (self.utf8_value << 6) | (byte & 0x3F)
        self.utf8_left -= 1
        if self.utf8_left:
            return
        codepoint = self.utf8_value
        if codepoint < self.utf8_min or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            raise InvalidRecipe("invalid UTF-8 in recipe JSON")
        self.capture(chr(codepoint))

    def _start_utf8(self, byte: int) -> None:
        if 0xC2 <= byte <= 0xDF:
            self.utf8_left, self.utf8_value, self.utf8_min = 1, byte & 0x1F, 0x80
        elif 0xE0 <= byte <= 0xEF:
            self.utf8_left, self.utf8_value, self.utf8_min = 2, byte & 0x0F, 0x800
        elif 0xF0 <= byte <= 0xF4:
            self.utf8_left, self.utf8_value, self.utf8_min = 3, byte & 0x07, 0x10000
        else:
            raise InvalidRecipe("invalid UTF-8 in recipe JSON")

    def _finish_string(self) -> None:
        self.mode = "default"
        frame = self.frame()
        if self.string_context == "key":
            frame.key = self.prefix if not self.truncated else ""
            if frame.key in _REQUIRED_KEYS.get(frame.scope, {}):
                if frame.key in frame.seen:
                    raise InvalidRecipe("duplicate recipe field")
                frame.seen.append(frame.key)
            frame.state = "colon"
        elif frame.scope == "root" and frame.key == "name":
            self.root_name = not self.truncated and self.prefix.lower() == self.adapter
        elif frame.scope == "field" and frame.key == "name":
            frame.name_valid = True
        elif frame.scope == "matches":
            if not self.prefix.startswith("https://"):
                raise InvalidRecipe("matches must contain only https:// patterns")
            self.match_count += 1

    def string(self, byte: int) -> None:
        if self.utf8_left:
            self._decode_utf8_continuation(byte)
            return
        if byte == _QUOTE:
            self._finish_string()
            return
        if byte == _BACKSLASH:
            self.mode = "escape"
        elif byte < _FIRST_PRINTABLE:
            raise InvalidRecipe("control character in recipe JSON string")
        elif byte < 128:
            self.capture(chr(byte))
        else:
            self._start_utf8(byte)

    def number(self, byte: int) -> bool:
        next_state = _next_number_state(self.number_state, byte)
        if next_state is None:
            self.mode = "default"
            return False
        self.number_state = next_state
        return True

    def _step_escape(self, byte: int) -> None:
        if byte == _UNICODE_ESCAPE:
            self.mode = "unicode"
            self.unicode_digits = ""
        elif byte in _ESCAPES:
            self.capture(_ESCAPES[byte])
            self.mode = "string"
        else:
            raise InvalidRecipe("invalid escape in recipe JSON")

    def _step_unicode(self, byte: int) -> None:
        if byte not in _HEX:
            raise InvalidRecipe("invalid Unicode escape in recipe JSON")
        self.unicode_digits += chr(byte)
        if len(self.unicode_digits) == 4:
            self.capture(chr(int(self.unicode_digits, 16)))
            self.mode = "string"

    def _step_literal(self, byte: int) -> None:
        if self.literal_pos >= len(self.literal) or byte != ord(self.literal[self.literal_pos]):
            raise InvalidRecipe("invalid recipe JSON literal")
        self.literal_pos += 1
        if self.literal_pos == len(self.literal):
            self.mode = "default"

    def _step_object(self, frame: Frame, byte: int) -> None:
        if frame.state in ("key_or_end", "key"):
            if byte == _CLOSE_OBJECT and frame.state == "key_or_end":
                self.pop()
            elif byte == _QUOTE:
                self.start_string("key")
            else:
                raise InvalidRecipe("invalid recipe JSON object")
        elif frame.state == "colon":
            if byte != _COLON:
                raise InvalidRecipe("invalid recipe JSON object")
            frame.state = "value"
        elif frame.state == "value":
            self.start_value(byte)
        elif byte == _COMMA:
            frame.state = "key"
        elif byte == _CLOSE_OBJECT:
            self.pop()
        else:
            raise InvalidRecipe("invalid recipe JSON object")

    def _step_array(self, frame: Frame, byte: int) -> None:
        if frame.state in ("value_or_end", "value"):
            if byte == _CLOSE_ARRAY and frame.state == "value_or_end":
                self.pop()
            else:
                self.start_value(byte)
        elif byte == _COMMA:
            frame.state = "value"
        elif byte == _CLOSE_ARRAY:
            self.pop()
        else:
            raise InvalidRecipe("invalid recipe JSON array")

    def step(self, byte: int) -> None:
        while True:
            if self.mode == "root":
                if byte in _SPACE:
                    return
                if byte != _OPEN_OBJECT:
                    raise InvalidRecipe("a recipe is an object")
                self.mode = "default"
                return
            if self.mode == "string":
                self.string(byte)
                return
            if self.mode == "escape":
                self._step_escape(byte)
                return
            if self.mode == "unicode":
                self._step_unicode(byte)
                return
            if self.mode == "number":
                if self.number(byte):
                    return
                continue
            if self.mode == "literal":
                self._step_literal(byte)
                return
            if byte in _SPACE:
                return
            if self.depth == 0:
                raise InvalidRecipe("trailing data after recipe JSON")
            frame = self.frame()
            if frame.kind == "object":
                self._step_object(frame, byte)
            else:
                self._step_array(frame, byte)
            return

    def feed(self, chunk: bytes) -> None:
        for byte in chunk:
            self.step(byte)

    def finish(self) -> None:
        if self.mode == "number" and self.number_state in _COMPLETE_NUMBER_STATES:
            self.mode = "default"
        if (
            self.mode != "default"
            or self.depth
            or not (self.root_name and self.root_matches and self.root_fields and self.match_count)
        ):
            raise InvalidRecipe("recipe is incomplete or missing a required field")

    def checkpoint(self) -> dict:
        return {key: getattr(self, key) for key in _CHECKPOINT_DEFAULTS}

    def changed_frames(self) -> dict[int, dict]:
        return {index: asdict(frame) for index, frame in self.frames.items() if index < self.depth}
