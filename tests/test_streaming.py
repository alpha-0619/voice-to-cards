"""The partial-JSON scanner is the load-bearing piece of the streaming path.

These tests are written to fail rather than to pass: every case is a fragment
boundary or an encoding shape that a naive `json.loads(buffer + "}")` approach
gets wrong. Chunking is exhaustive where it is cheap to be -- if a boundary can
fall inside an escape sequence, some caller eventually puts it there.
"""

from __future__ import annotations

import json

import pytest

from core.streaming import (
    FieldDelta,
    FieldDone,
    PartialJSONObject,
    parse_complete,
)


def drain(payload: str, chunk_size: int) -> list:
    parser = PartialJSONObject()
    events = []
    for i in range(0, len(payload), chunk_size):
        events.extend(parser.feed(payload[i : i + chunk_size]))
    events.extend(parser.close())
    return events


def text_of(events: list, key: str) -> str:
    return "".join(e.text for e in events if isinstance(e, FieldDelta) and e.key == key)


def completed(events: list) -> dict:
    return {e.key: e.value for e in events if isinstance(e, FieldDone)}


PAYLOAD = json.dumps(
    {
        "understood": "You asked about flight MA1204.",
        "language": "en",
        "action": "get_flight_status",
        "arguments": {"flight_number": "MA1204", "nested": {"deep": [1, 2]}},
        "confidence": 0.87,
        "urgent": False,
        "note": None,
    }
)


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 13, 64, 4096])
def test_every_chunking_yields_the_same_object(chunk_size: int) -> None:
    """Fragment boundaries must not change the decoded result."""
    events = drain(PAYLOAD, chunk_size)
    assert completed(events) == json.loads(PAYLOAD)


@pytest.mark.parametrize("chunk_size", [1, 3, 8, 4096])
def test_string_deltas_reconstruct_the_string(chunk_size: int) -> None:
    """The incremental text a UI renders must equal the final value."""
    events = drain(PAYLOAD, chunk_size)
    assert text_of(events, "understood") == json.loads(PAYLOAD)["understood"]


def test_first_field_completes_before_later_fields_start() -> None:
    """The reason the whole design works: the opening restatement is readable
    before the rest of the decision exists. If field order stopped being
    honoured, perceived latency would silently regress to batch behaviour."""
    events = drain(PAYLOAD, 1)
    first_done = next(i for i, e in enumerate(events) if isinstance(e, FieldDone))
    assert events[first_done].key == "understood"
    later = [e.key for e in events[first_done + 1 :] if isinstance(e, FieldDone)]
    assert later[0] == "language"
    # And text for `understood` arrived before anything else finished at all.
    assert isinstance(events[0], FieldDelta) and events[0].key == "understood"


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 5, 6, 7, 8])
def test_escape_sequences_split_across_fragments(chunk_size: int) -> None:
    """A boundary inside `\\uXXXX` is the classic way a hand-rolled parser
    corrupts non-English output."""
    value = 'Rötar "hızlı" \\ tab:\there\nnewline é ñ 日本語'
    payload = json.dumps({"understood": value})
    events = drain(payload, chunk_size)
    assert completed(events)["understood"] == value
    assert text_of(events, "understood") == value


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 6, 11])
def test_surrogate_pairs_are_never_emitted_half_decoded(chunk_size: int) -> None:
    """Non-BMP characters arrive as two escapes. Emitting the high half alone
    puts a lone surrogate on the wire, which breaks JSON re-encoding downstream."""
    value = "boarding 🛫 done ✅ 𝔘nicode"
    payload = json.dumps({"understood": value}, ensure_ascii=True)
    assert "\\ud83d" in payload.lower()  # the shape under test really is present
    events = drain(payload, chunk_size)
    streamed = text_of(events, "understood")
    assert streamed == value
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in streamed)


def test_braces_inside_nested_strings_do_not_close_the_object() -> None:
    """Depth tracking has to be string-aware or a `}` in user text truncates
    the argument bag."""
    payload = json.dumps({"arguments": {"note": "closing } brace [ and bracket"}, "action": "x"})
    events = drain(payload, 1)
    assert completed(events)["arguments"] == {"note": "closing } brace [ and bracket"}
    assert completed(events)["action"] == "x"


def test_empty_object_and_empty_string_value() -> None:
    assert completed(drain("{}", 1)) == {}
    assert completed(drain('{"understood":""}', 1)) == {"understood": ""}


def test_whitespace_between_tokens_is_tolerated() -> None:
    payload = '{\n  "a" : "x" ,\n  "b" : { "c" : 1 }\n}'
    assert completed(drain(payload, 1)) == {"a": "x", "b": {"c": 1}}


def test_truncated_stream_is_an_error_not_a_partial_answer() -> None:
    """A dropped connection must not produce a card built from half a decision."""
    with pytest.raises(ValueError):
        parse_complete('{"understood": "half a sen')


def test_malformed_escape_is_rejected() -> None:
    parser = PartialJSONObject()
    with pytest.raises(ValueError):
        list(parser.feed('{"a":"\\q"}'))


def test_trailing_content_after_object_is_rejected() -> None:
    parser = PartialJSONObject()
    with pytest.raises(ValueError):
        list(parser.feed('{"a":1} {"b":2}'))


def test_parse_complete_matches_stdlib_on_a_realistic_payload() -> None:
    assert parse_complete(PAYLOAD) == json.loads(PAYLOAD)
