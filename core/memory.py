"""Carrying the last few turns without carrying them into the wrong answer.

The hard part of session memory is not remembering. It is *not* bleeding: when
someone asks about a flight, then changes the subject to lounge access, the
flight number must not follow them into the second answer. A card built from a
stale identifier looks confident and is wrong, which is worse than a card that
asks for the identifier again.

Two defences, one in each layer:

  * here -- the transcript handed to the model is sanitised into a strict,
    bounded, alternating shape, so nothing malformed or unbounded reaches it;
  * in the router prompt -- an explicit instruction that prior turns may only
    resolve a back-reference in the *current* sentence, and that a missing
    argument must be omitted rather than filled in from earlier context.

Prior turns are replayed as plain text, never as the original tool-call blocks:
the API requires every `tool_use` to be followed by a matching `tool_result`,
so replaying old calls verbatim is not just wasteful, it is invalid.
"""

from __future__ import annotations

from typing import Any

MAX_CONTENT_CHARS = 600


def sanitize_history(history: Any, max_turns: int = 6) -> list[dict[str, str]]:
    """Coerce whatever the client sent into a transcript the API will accept.

    Guarantees on the result: alternating roles, starts with `user`, ends with
    `assistant`, at most `max_turns` exchanges, each entry a bounded string.
    Anything that does not fit is dropped rather than repaired -- a malformed
    transcript is a client bug, and silently inventing turns to patch it would
    put words in the caller's mouth.
    """
    if not isinstance(history, list):
        return []

    cleaned: list[dict[str, str]] = []
    for turn in history:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip()
        content = turn.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        # Collapse a non-alternating run instead of interleaving a filler turn.
        if cleaned and cleaned[-1]["role"] == role:
            continue
        cleaned.append({"role": role, "content": content.strip()[:MAX_CONTENT_CHARS]})

    while cleaned and cleaned[0]["role"] != "user":
        cleaned.pop(0)
    while cleaned and cleaned[-1]["role"] != "assistant":
        cleaned.pop()

    return cleaned[-(max_turns * 2) :]
