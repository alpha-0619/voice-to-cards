"""Reading a forced tool call while it is still being written.

The router asks the model for exactly one tool call whose arguments carry the
whole routing decision. That is what makes the decision impossible to
mis-parse -- but a tool call is JSON, and JSON is only valid once it is
finished. Waiting for the closing brace means waiting for the entire response
before showing anything.

So we don't wait. The API streams tool arguments as `input_json_delta`
fragments; this module walks those fragments character by character and hands
back each top-level field as it arrives, including the *partial* text of a
string field still being written. Pair that with a tool schema whose first
property is the one-line restatement of what the caller asked for, and the
first words reach the screen about as fast as the model can produce them,
while the rest of the decision is still forming.

The parser is deliberately narrow: one flat JSON object, top-level fields only.
Nested values (a tool's argument bag) are captured raw and parsed with the
standard library once their braces balance. Anything malformed raises rather
than guessing -- a truncated stream must surface as an error, not as a card
built from half a decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_WHITESPACE = " \t\r\n"


@dataclass(frozen=True)
class FieldDelta:
    """More text for a string field that is still being written."""

    key: str
    text: str


@dataclass(frozen=True)
class FieldDone:
    """A top-level field is complete. `value` is fully decoded."""

    key: str
    value: Any


StreamEvent = FieldDelta | FieldDone


class PartialJSONObject:
    """Incrementally decode one flat JSON object from arbitrary fragments.

    Feed it whatever the transport hands you -- fragment boundaries can fall
    anywhere, including the middle of a `\\uXXXX` escape or a surrogate pair --
    and it yields events as soon as they are unambiguous.
    """

    # Scanner states.
    _SEEK_OBJECT = "seek_object"
    _SEEK_KEY = "seek_key"
    _IN_KEY = "in_key"
    _SEEK_COLON = "seek_colon"
    _SEEK_VALUE = "seek_value"
    _IN_STRING = "in_string"
    _IN_LITERAL = "in_literal"
    _IN_NESTED = "in_nested"
    _DONE = "done"

    def __init__(self) -> None:
        self._state = self._SEEK_OBJECT
        self._key = ""
        self._buf: list[str] = []          # decoded string value, or raw literal/nested
        self._pending_escape = ""          # partial "\\uXXXX" spanning a fragment boundary
        self._high_surrogate: int | None = None
        self._depth = 0                    # nesting depth inside a captured value
        self._nested_in_string = False     # so braces inside nested strings don't count
        self._nested_escape = False

    @property
    def done(self) -> bool:
        return self._state == self._DONE

    def feed(self, fragment: str) -> Iterator[StreamEvent]:
        for char in fragment:
            yield from self._step(char)

    def close(self) -> Iterator[StreamEvent]:
        """Flush a bare literal that ran to the end of input without a delimiter.

        Well-formed input always closes with `}`, which finishes any pending
        literal, so this only fires on truncation.
        """
        if self._state == self._IN_LITERAL:
            yield from self._finish_literal()
            self._state = self._DONE

    # -- scanner ---------------------------------------------------------

    def _step(self, char: str) -> Iterator[StreamEvent]:
        state = self._state

        if state == self._SEEK_OBJECT:
            if char in _WHITESPACE:
                return
            if char != "{":
                raise ValueError(f"expected object, got {char!r}")
            self._state = self._SEEK_KEY

        elif state == self._SEEK_KEY:
            if char in _WHITESPACE or char == ",":
                return
            if char == "}":
                self._state = self._DONE
                return
            if char != '"':
                raise ValueError(f"expected key, got {char!r}")
            self._key = ""
            self._buf = []
            self._state = self._IN_KEY

        elif state == self._IN_KEY:
            # Keys come from our own schema, so they never carry escapes; a
            # plain scan is enough and keeps the hot path simple.
            if char == '"':
                self._key = "".join(self._buf)
                self._buf = []
                self._state = self._SEEK_COLON
            else:
                self._buf.append(char)

        elif state == self._SEEK_COLON:
            if char in _WHITESPACE:
                return
            if char != ":":
                raise ValueError(f"expected ':' after key {self._key!r}, got {char!r}")
            self._state = self._SEEK_VALUE

        elif state == self._SEEK_VALUE:
            if char in _WHITESPACE:
                return
            self._buf = []
            if char == '"':
                self._pending_escape = ""
                self._high_surrogate = None
                self._state = self._IN_STRING
            elif char in "{[":
                self._buf.append(char)
                self._depth = 1
                self._nested_in_string = False
                self._nested_escape = False
                self._state = self._IN_NESTED
            else:
                self._buf.append(char)
                self._state = self._IN_LITERAL

        elif state == self._IN_STRING:
            yield from self._step_string(char)

        elif state == self._IN_LITERAL:
            if char in ",}" :
                yield from self._finish_literal()
                self._state = self._DONE if char == "}" else self._SEEK_KEY
            else:
                self._buf.append(char)

        elif state == self._IN_NESTED:
            yield from self._step_nested(char)

        elif state == self._DONE:
            if char not in _WHITESPACE:
                raise ValueError(f"trailing content after object: {char!r}")

    def _step_string(self, char: str) -> Iterator[StreamEvent]:
        # Mid-escape: keep collecting until the sequence resolves. Doing this
        # per character is what lets a fragment boundary land inside "\\u00e9".
        if self._pending_escape:
            self._pending_escape += char
            decoded = self._resolve_escape()
            if decoded is not None:
                self._buf.append(decoded)
                yield FieldDelta(self._key, decoded)
            return

        if char == "\\":
            self._pending_escape = "\\"
            return

        if char == '"':
            if self._high_surrogate is not None:
                raise ValueError("string ended on an unpaired surrogate")
            yield FieldDone(self._key, "".join(self._buf))
            self._buf = []
            self._state = self._SEEK_KEY
            return

        if self._high_surrogate is not None:
            raise ValueError("unpaired high surrogate followed by literal text")

        self._buf.append(char)
        yield FieldDelta(self._key, char)

    def _resolve_escape(self) -> str | None:
        """Return the decoded character, or None while the escape is incomplete."""
        seq = self._pending_escape
        marker = seq[1]

        if marker != "u":
            if marker not in _SIMPLE_ESCAPES:
                raise ValueError(f"invalid escape sequence: {seq!r}")
            if self._high_surrogate is not None:
                raise ValueError("unpaired high surrogate followed by an escape")
            self._pending_escape = ""
            return _SIMPLE_ESCAPES[marker]

        if len(seq) < 6:  # "\uXXXX"
            return None
        try:
            code = int(seq[2:6], 16)
        except ValueError as exc:
            raise ValueError(f"invalid unicode escape: {seq!r}") from exc
        self._pending_escape = ""

        # Non-BMP characters (emoji, and plenty of CJK extensions) arrive as a
        # surrogate pair. Emitting each half on its own would push lone
        # surrogates into the UI, so hold the high half until its partner lands.
        if 0xD800 <= code <= 0xDBFF:
            if self._high_surrogate is not None:
                raise ValueError("two consecutive high surrogates")
            self._high_surrogate = code
            return None
        if 0xDC00 <= code <= 0xDFFF:
            if self._high_surrogate is None:
                raise ValueError("low surrogate with no preceding high surrogate")
            combined = 0x10000 + ((self._high_surrogate - 0xD800) << 10) + (code - 0xDC00)
            self._high_surrogate = None
            return chr(combined)
        if self._high_surrogate is not None:
            raise ValueError("high surrogate not followed by a low surrogate")
        return chr(code)

    def _step_nested(self, char: str) -> Iterator[StreamEvent]:
        self._buf.append(char)

        if self._nested_escape:
            self._nested_escape = False
            return
        if self._nested_in_string:
            if char == "\\":
                self._nested_escape = True
            elif char == '"':
                self._nested_in_string = False
            return
        if char == '"':
            self._nested_in_string = True
            return
        if char in "{[":
            self._depth += 1
            return
        if char in "}]":
            self._depth -= 1
            if self._depth == 0:
                raw = "".join(self._buf)
                self._buf = []
                self._state = self._SEEK_KEY
                yield FieldDone(self._key, json.loads(raw))

    def _finish_literal(self) -> Iterator[StreamEvent]:
        raw = "".join(self._buf).strip()
        self._buf = []
        yield FieldDone(self._key, json.loads(raw))


def parse_complete(text: str) -> dict:
    """Convenience for the non-streaming path and for tests.

    Runs the same scanner over a finished payload so both paths share one
    decoder; a divergence between them would be the kind of bug that only
    shows up under load.
    """
    parser = PartialJSONObject()
    out: dict[str, Any] = {}
    for event in parser.feed(text):
        if isinstance(event, FieldDone):
            out[event.key] = event.value
    for event in parser.close():
        if isinstance(event, FieldDone):
            out[event.key] = event.value
    if not parser.done:
        raise ValueError("incomplete JSON object")
    return out
